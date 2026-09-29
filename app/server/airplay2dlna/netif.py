"""网络接口探测与 LAN 接口择优。

飞牛 NAS 上通常同时存在物理网卡、Docker 网桥（docker0/br-*）、Overlay（ovs）、
VPN（tailscale0/tun0/zt*）、veth 等接口。AirPlay 的 mDNS 与 SSDP 只能走真正的
LAN 接口，否则 iPhone 看不到本机、DLNA 设备也发现不了。

本模块不依赖 psutil，直接读取 ``/proc/net`` 与 ``/sys``，并用 ``SIOCGIFADDR``
ioctl 兜底。
"""

from __future__ import annotations

import fcntl
import ipaddress
import logging
import socket
import struct
from dataclasses import dataclass
from typing import Iterable

log = logging.getLogger("netif")

# 明确排除的接口名前缀/精确名：容器、虚拟、VPN、隧道
_EXCLUDED_PREFIXES = (
    "lo",
    "docker",
    "br-",
    "veth",
    "virbr",
    "ovs",
    "tailscale",
    "tun",
    "tap",
    "wg",
    "zt",
    "zerotier",
    "vmnet",
    "vboxnet",
    "dummy",
    "bond0.1",
)

# OVS 模式下真实物理网卡会以 <name>-ovs 的形式出现，需要当 LAN 接口处理
_OVS_SUFFIX = "-ovs"

SIOCGIFADDR = 0x8915


@dataclass(frozen=True)
class Interface:
    name: str
    address: str
    netmask: str
    is_physical: bool
    has_default_route: bool
    is_up: bool

    @property
    def prefixlen(self) -> int:
        try:
            return ipaddress.IPv4Network(f"0.0.0.0/{self.netmask}").prefixlen
        except (ValueError, TypeError):
            return 24


def _ifname_is_excluded(name: str) -> bool:
    lowered = name.lower()
    for prefix in _EXCLUDED_PREFIXES:
        if lowered == prefix or lowered.startswith(prefix):
            # 但物理网卡的 -ovs 形式要保留
            if lowered.endswith(_OVS_SUFFIX):
                return False
            return True
    return False


def _read_proc_net_route() -> tuple[list[tuple[str, str]], set[str]]:
    """返回 (默认路由列表 [(iface, gateway)], 所有出现过的接口名集合)。"""
    defaults: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        with open("/proc/net/route", "r", encoding="ascii") as handle:
            next(handle, None)
            for line in handle:
                parts = line.split()
                if len(parts) < 3:
                    continue
                iface, dest, gateway = parts[0], parts[1], parts[2]
                seen.add(iface)
                if dest == "00000000":
                    defaults.append((iface, gateway))
    except OSError as exc:
        log.debug("读取 /proc/net/route 失败: %s", exc)
    return defaults, seen


def _read_ipv4_addresses() -> dict[str, tuple[str, str]]:
    """通过 ioctl 获取接口 IPv4 地址与掩码（不依赖 iproute2）。"""
    result: dict[str, tuple[str, str]] = {}
    try:
        names = socket.if_nameindex()
    except OSError:
        return result
    control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _index, name in names:
            try:
                packed = struct.pack("256s", name[:15].encode("ascii"))
                addr = socket.inet_ntoa(fcntl.ioctl(control.fileno(), SIOCGIFADDR, packed)[20:24])
                # 掩码需要单独的 SIOCGIFNETMASK
                netmask = "255.255.255.0"
                try:
                    SIOCGIFNETMASK = 0x891B
                    mask_raw = fcntl.ioctl(control.fileno(), SIOCGIFNETMASK, packed)[20:24]
                    netmask = socket.inet_ntoa(mask_raw)
                except OSError:
                    pass
                result[name] = (addr, netmask)
            except OSError:
                continue
    finally:
        control.close()
    return result


def _is_up(name: str) -> bool:
    try:
        with open(f"/sys/class/net/{name}/operstate", "r", encoding="ascii") as handle:
            state = handle.read().strip()
        return state in ("up", "unknown")
    except OSError:
        return True


def _is_physical(name: str) -> bool:
    """判断是否为物理网卡（存在 device 符号链接）。"""
    import os

    return os.path.isdir(f"/sys/class/net/{name}/device") or os.path.islink(
        f"/sys/class/net/{name}/device"
    )


def list_interfaces() -> list[Interface]:
    """枚举所有带 IPv4 地址的接口。"""
    defaults, _seen = _read_proc_net_route()
    default_ifaces = {iface for iface, _gw in defaults}
    addresses = _read_ipv4_addresses()

    result: list[Interface] = []
    for name, (addr, netmask) in sorted(addresses.items()):
        base_name = name[:-4] if name.endswith(_OVS_SUFFIX) else name
        result.append(
            Interface(
                name=name,
                address=addr,
                netmask=netmask,
                is_physical=_is_physical(base_name) or _is_physical(name),
                has_default_route=name in default_ifaces or base_name in default_ifaces,
                is_up=_is_up(name),
            )
        )
    return result


def _score(iface: Interface, default_route_iface: str | None) -> int:
    """给接口打分，分高者优先。"""
    score = 0
    if iface.has_default_route:
        score += 100
    if iface.name == default_route_iface:
        score += 50
    if iface.is_physical:
        score += 30
    if iface.name.endswith(_OVS_SUFFIX):
        score += 20  # OVS 桥接的物理口在飞牛上就是实际 LAN 出口
    try:
        addr = ipaddress.IPv4Address(iface.address)
    except ipaddress.AddressValueError:
        return -1000
    if addr.is_loopback or addr.is_link_local:
        return -1000
    if addr.is_private:
        score += 10
    # 常见容器网段降权
    if addr in ipaddress.IPv4Network("172.16.0.0/12") and not iface.is_physical:
        score -= 40
    return score


def select_lan_addresses(limit: int = 2) -> list[str]:
    """返回推荐的 LAN 源地址列表，最优在前。

    任务书第 17 节要求：不要把服务错误地绑定到 loopback，也不要假设只有一个固定网卡。
    """
    interfaces = list_interfaces()
    defaults, _ = _read_proc_net_route()
    default_route_iface = defaults[0][0] if defaults else None

    candidates = [i for i in interfaces if not _ifname_is_excluded(i.name)]
    if not candidates:
        log.warning("未找到可用的 LAN 接口，退回全部非 loopback 接口")
        candidates = [i for i in interfaces if not i.address.startswith("127.")]

    scored = sorted(
        candidates, key=lambda i: _score(i, default_route_iface), reverse=True
    )
    usable = [i for i in scored if _score(i, default_route_iface) > -1000]
    if not usable:
        log.warning("所有接口评分都不可用，退回第一个非 loopback 地址")
        usable = candidates[:1]

    addresses: list[str] = []
    for iface in usable:
        if iface.address not in addresses:
            addresses.append(iface.address)
        if len(addresses) >= limit:
            break
    log.info(
        "网络接口择优结果: %s",
        ", ".join(f"{i.name}={i.address}(score={_score(i, default_route_iface)})" for i in scored[:5]),
    )
    return addresses


def primary_lan_address() -> str:
    """返回对外公告用的主 LAN 地址（用于告诉 DLNA 设备去哪里拉流）。"""
    addresses = select_lan_addresses(limit=1)
    if addresses:
        return addresses[0]
    # 兜底：用 UDP connect 让内核选源地址（不会真的发包）
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("223.5.5.5", 53))
            return probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return "127.0.0.1"


def address_for_remote(remote_ip: str) -> str:
    """选择能与 ``remote_ip`` 通信的本机源地址（多网段时必须选对）。"""
    try:
        target = ipaddress.ip_address(remote_ip)
    except ValueError:
        return primary_lan_address()

    best: tuple[int, str] | None = None
    for iface in list_interfaces():
        try:
            local = ipaddress.ip_address(iface.address)
        except ValueError:
            continue
        if local.version != target.version:
            continue
        network = ipaddress.ip_network(f"{iface.address}/{iface.netmask}", strict=False)
        if target in network:
            score = 100 + (50 if iface.has_default_route else 0)
            if best is None or score > best[0]:
                best = (score, iface.address)
    return best[1] if best else primary_lan_address()


def iter_lan_interfaces() -> Iterable[Interface]:
    for iface in list_interfaces():
        if not _ifname_is_excluded(iface.name) and not iface.address.startswith("127."):
            yield iface
