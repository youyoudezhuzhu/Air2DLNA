"""SSDP（Simple Service Discovery Protocol）发现模块。

本模块仅依赖 Python 标准库，负责：

* 构造 M-SEARCH 请求（严格的 CRLF 行尾，空行结尾）；
* 在给定的本地 IPv4 接口上发送组播搜索并收集单播响应；
* 防御式解析响应报文（大小写不敏感、兼容 LF-only 行尾）；
* 按 ``(udn, location)`` 去重并合并同一设备的多条响应。

设计约束：
    * 所有网络调用均设置超时；
    * :func:`discover` 绝不向调用方抛出异常，任何错误都通过 ``log`` 回调记录；
    * 每个返回的 :class:`SsdpResponse` 都带有响应来源的源 IP 地址。
"""

from __future__ import annotations

import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

#: MediaRenderer 设备类型（SSDP 搜索目标）。
MEDIA_RENDERER_ST = "urn:schemas-upnp-org:device:MediaRenderer:1"
#: SSDP 组播地址。
SSDP_ADDR = "239.255.255.250"
#: SSDP 组播端口。
SSDP_PORT = 1900
#: 兜底搜索目标（部分设备只对 rootdevice / ssdp:all 作出响应）。
SSDP_ALL_ST = "ssdp:all"
#: M-SEARCH 的 MX 头取值（秒）。
SSDP_MX = 2

#: 单个 UDP 报文最大读取长度。
_MAX_DATAGRAM = 65535

#: 日志回调类型别名：接收单条字符串消息，可为 None。
LogFunc = Callable[[str], None] | None


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class SsdpResponse:
    """一条 SSDP 搜索响应。

    字段保存原始头值（不做归一化，除了 ``raw`` 的键统一为小写）。
    """

    #: LOCATION 头的绝对 URL；缺失时为空字符串。
    location: str
    #: USN 头的原始值。
    usn: str
    #: ST 头的原始值。
    st: str
    #: SERVER 头的原始值；缺失时为空字符串。
    server: str
    #: 响应来源 IP 地址。
    ip: str
    #: 全部响应头，键为小写、值为原始文本。
    raw: dict = field(default_factory=dict)

    @property
    def udn(self) -> str:
        """从 USN 中提取 UDN。

        规则：截断第一个 ``::`` 及其之后的全部内容。
        例如 ``'uuid:abc::urn:schemas-upnp-org:device:MediaRenderer:1'``
        得到 ``'uuid:abc'``。
        """
        usn = self.usn or ""
        return usn.split("::", 1)[0].strip()


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _log(log: LogFunc, message: str) -> None:
    """安全地调用日志回调：回调缺失或自身抛错都不影响主流程。"""
    if log is None:
        return
    try:
        log(str(message))
    except Exception:  # noqa: BLE001 - 日志失败绝不能影响发现流程
        pass


def build_m_search(st: str) -> bytes:
    """构造一个 M-SEARCH 请求报文（CRLF 行尾，以空行结束）。

    :param st: 搜索目标（ST 头值）。
    :return: 可直接通过 UDP 发送的字节串。
    """
    request = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {SSDP_MX}\r\n"
        f"ST: {'' if st is None else st}\r\n"
        "\r\n"
    )
    return request.encode("utf-8")


def parse_response(data: bytes, ip: str) -> SsdpResponse | None:
    """解析一条 SSDP 响应报文。

    防御式解析：

    * 状态行可以是 ``HTTP/1.1 200 OK``（也可能缺失）；
    * 头名称大小写不敏感；
    * 同时兼容 ``\\r\\n`` 与 ``\\n`` 行尾；
    * 无法解析出任何头时返回 ``None``。

    :param data: UDP 收到的原始字节。
    :param ip: 响应来源 IP。
    :return: 解析结果，失败时为 ``None``。
    """
    if not data:
        return None
    try:
        text = data.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - 解码异常按无法解析处理
        return None

    # 统一换行后再切分，兼容 LF-only 报文。
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if not lines:
        return None

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        name, sep, value = line.partition(":")
        if not sep:
            continue
        name = name.strip().lower()
        if not name:
            continue
        headers[name] = value.strip()

    if not headers:
        # 既不是有效状态行也没有任何头，视为噪声报文。
        return None

    return SsdpResponse(
        location=headers.get("location", ""),
        usn=headers.get("usn", ""),
        st=headers.get("st", ""),
        server=headers.get("server", ""),
        ip=ip or "",
        raw=headers,
    )


def _merge_response(target: SsdpResponse, other: SsdpResponse) -> None:
    """把 ``other`` 合并进 ``target``，只填补 target 中的空值。"""
    if not target.location and other.location:
        target.location = other.location
    if not target.usn and other.usn:
        target.usn = other.usn
    if not target.st and other.st:
        target.st = other.st
    if not target.server and other.server:
        target.server = other.server
    if not target.ip and other.ip:
        target.ip = other.ip
    for key, value in (other.raw or {}).items():
        if not target.raw.get(key):
            target.raw[key] = value


def _new_socket(*args, **kwargs) -> socket.socket:
    """创建 UDP socket 的薄封装（便于单元测试注入故障）。"""
    return socket.socket(*args, **kwargs)


def _enumerate_ipv4() -> list[str]:
    """枚举主机上的非回环 IPv4 地址。

    依次尝试三种标准库手段，任一成功即纳入结果：

    1. 通过 UDP ``connect``（不发送数据包）取得默认路由出口地址；
    2. Linux 下的 ``SIOCGIFCONF`` ioctl；
    3. 解析主机名。

    若一无所获，返回 ``['0.0.0.0']`` 以便调用方仍可尝试发送。
    """
    found: list[str] = []

    def _add(ip: str) -> None:
        if ip and ip not in found and not ip.startswith("127."):
            found.append(ip)

    # 1) 默认路由地址。
    try:
        with _new_socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as probe:
            probe.connect(("8.8.8.8", 53))
            _add(str(probe.getsockname()[0]))
    except Exception:  # noqa: BLE001
        pass

    # 2) Linux SIOCGIFCONF：遍历所有已配置地址。
    try:
        import array
        import fcntl

        max_bytes = 8192
        buf = array.array("B", b"\0" * max_bytes)
        with _new_socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            result = fcntl.ioctl(
                sock.fileno(),
                0x8912,  # SIOCGIFCONF
                struct.pack("iL", max_bytes, buf.buffer_info()[0]),
            )
        raw_len = struct.unpack("i", result[:4])[0]
        data = buf.tobytes()[:raw_len]
        # struct ifreq 在 Linux 上为 40 字节：16 字节接口名 + 16 字节 sockaddr。
        for offset in range(0, len(data) - 23, 40):
            sockaddr = data[offset + 16 : offset + 24]
            # sockaddr_in: family(2) + port(2) + addr(4)
            _add(socket.inet_ntoa(sockaddr[4:8]))
    except Exception:  # noqa: BLE001 - 非 Linux 或权限受限时静默跳过
        pass

    # 3) 主机名解析。
    try:
        for info in socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_INET, socket.SOCK_DGRAM
        ):
            _add(str(info[4][0]))
    except Exception:  # noqa: BLE001
        pass

    if not found:
        found.append("0.0.0.0")
    return found


def _select_interfaces() -> list[str]:
    """确定用于发送 M-SEARCH 的本地 IPv4 地址列表。

    优先使用 ``netif.select_lan_addresses()``（若该模块可导入且调用成功），
    否则回退到 :func:`_enumerate_ipv4`。
    """
    select = None
    from importlib import import_module

    for module_name in (".netif", "netif"):
        try:
            if module_name.startswith("."):
                module = import_module(module_name, __package__)
            else:
                module = import_module(module_name)
        except Exception:  # noqa: BLE001 - netif 为可选依赖
            continue
        candidate = getattr(module, "select_lan_addresses", None)
        if callable(candidate):
            select = candidate
            break

    if select is not None:
        try:
            addresses = [str(item) for item in select() if item]
            if addresses:
                return addresses
        except Exception as exc:  # noqa: BLE001 - netif 失败时回退到本地枚举
            _log(None, f"ssdp: netif.select_lan_addresses() failed: {exc}")

    return _enumerate_ipv4()


def _search_once(interface: str, st: str, timeout: float, log: LogFunc) -> list[SsdpResponse]:
    """在单个接口上发送一次 M-SEARCH 并收集本窗口内的全部响应。

    :param interface: 本地 IPv4 地址；``''``/``'0.0.0.0'`` 表示不指定出口接口。
    :param st: 搜索目标。
    :param timeout: 接收窗口（秒），同时作为 socket 超时。
    :param log: 日志回调。
    :return: 解析成功的响应列表（可能为空）。
    """
    request = build_m_search(st)
    received: list[SsdpResponse] = []

    # 关键：SSDP 的响应是**单播回到 M-SEARCH 的源端口**的，因此必须用**同一个
    # socket** 发送与接收。早期实现把发送与接收拆成两个 socket，响应发到了发送
    # socket 的临时端口而无人读取，导致永远收不到任何设备（真实 bug）。
    try:
        with _new_socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except OSError:
                pass
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, SSDP_MX)
            except OSError as exc:
                _log(log, f"ssdp: IP_MULTICAST_TTL on {interface} failed: {exc}")
            if interface and interface != "0.0.0.0":
                try:
                    sock.setsockopt(
                        socket.IPPROTO_IP,
                        socket.IP_MULTICAST_IF,
                        socket.inet_aton(interface),
                    )
                except OSError as exc:
                    _log(log, f"ssdp: IP_MULTICAST_IF {interface} failed: {exc}")
                try:
                    # 显式绑定到该接口地址，确保源 IP 正确、响应能回到本 socket
                    sock.bind((interface, 0))
                except OSError:
                    sock.bind(("", 0))
            else:
                sock.bind(("", 0))

            try:
                sock.sendto(request, (SSDP_ADDR, SSDP_PORT))
            except OSError as exc:
                _log(log, f"ssdp: sendto {SSDP_ADDR}:{SSDP_PORT} via {interface} failed: {exc}")
                return received

            deadline = time.monotonic() + max(0.0, float(timeout))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    data, addr = sock.recvfrom(_MAX_DATAGRAM)
                except (socket.timeout, TimeoutError):
                    break
                except OSError as exc:
                    _log(log, f"ssdp: recvfrom failed on {interface}: {exc}")
                    break
                source_ip = str(addr[0]) if addr else ""
                response = parse_response(data, source_ip)
                if response is None:
                    _log(log, f"ssdp: ignoring unparseable datagram from {source_ip}")
                    continue
                received.append(response)
    except OSError as exc:
        _log(log, f"ssdp: cannot search on {interface}: {exc}")

    return received

# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------


def discover(
    timeout: float = 3.0,
    rounds: int = 3,
    interfaces: list[str] | None = None,
    log: LogFunc = None,
) -> list[SsdpResponse]:
    """发送 SSDP M-SEARCH 并收集响应。

    行为：

    * ``interfaces`` 为本地 IPv4 地址列表；为 ``None`` 时自动选择
      （优先 ``netif.select_lan_addresses()``，否则枚举主机非回环 IPv4）；
    * 每轮对**每个**接口发送一次 MediaRenderer 搜索；仅在第 2 轮额外发送
      一次 ``ssdp:all`` 作为兜底；
    * MX 固定为 2，每轮等待 ``timeout`` 秒，轮间休眠 ``0.5 * timeout`` 秒；
    * 若已收集到响应且本轮没有新增，则提前结束；
    * 按 ``(udn, location)`` 去重，重复项只填补首个非空值；
    * 任何网络错误都不会抛出，只通过 ``log`` 记录。

    :param timeout: 每轮接收窗口秒数。
    :param rounds: 最大轮数（至少 1）。
    :param interfaces: 本地 IPv4 地址列表，或 ``None`` 自动选择。
    :param log: 日志回调 ``log(str)``，可为 ``None``。
    :return: 去重后的响应列表；出错时返回已收集的部分。
    """
    try:
        total_rounds = max(1, int(rounds))
    except (TypeError, ValueError):
        total_rounds = 1
    try:
        window = max(0.0, float(timeout))
    except (TypeError, ValueError):
        window = 3.0

    if interfaces is None:
        try:
            interface_list: list[str] = _select_interfaces()
        except Exception as exc:  # noqa: BLE001 - 自动选择失败也不能抛出
            _log(log, f"ssdp: interface auto-selection failed: {exc}")
            interface_list = []
    else:
        try:
            interface_list = [str(item) for item in interfaces if item]
        except TypeError:
            interface_list = []

    interface_list = list(dict.fromkeys(interface_list))  # 去重且保持顺序

    if not interface_list:
        _log(log, "ssdp: no usable local interfaces, skipping discovery")
        return []

    _log(
        log,
        f"ssdp: discovering MediaRenderer on {interface_list} "
        f"(timeout={window}s, rounds={total_rounds})",
    )

    collected: dict[tuple[str, str], SsdpResponse] = {}
    order: list[tuple[str, str]] = []

    for round_index in range(total_rounds):
        new_this_round = 0
        search_targets: list[str] = [MEDIA_RENDERER_ST]
        if round_index == 1:
            search_targets.append(SSDP_ALL_ST)

        for interface in interface_list:
            for st in search_targets:
                try:
                    found: Iterable[SsdpResponse] = _search_once(interface, st, window, log)
                except Exception as exc:  # noqa: BLE001 - 单接口失败不影响其它接口
                    _log(log, f"ssdp: search via {interface} for {st} failed: {exc}")
                    continue
                for response in found:
                    key = (response.udn, response.location)
                    existing = collected.get(key)
                    if existing is None:
                        collected[key] = response
                        order.append(key)
                        new_this_round += 1
                    else:
                        _merge_response(existing, response)

        if collected and new_this_round == 0:
            _log(log, f"ssdp: round {round_index + 1} produced no new responses, stopping early")
            break

        if round_index < total_rounds - 1:
            try:
                time.sleep(0.5 * window)
            except Exception:  # noqa: BLE001 - 极端情况（被 signal 打断）也继续
                pass

    responses = [collected[key] for key in order]
    _log(log, f"ssdp: discovery finished, {len(responses)} unique response(s)")
    return responses
