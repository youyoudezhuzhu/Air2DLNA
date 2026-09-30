"""AirPlay 反向控制（DACP）与**逐项能力检测**。

对应 ARCHITECTURE_V2 第 14~17、19、20 节。

设计原则
--------

1. **能力必须如实上报**：每一项能力（play / pause / seek / next / previous）都有
   ``SUPPORTED`` / ``UNSUPPORTED`` / ``UNKNOWN`` 三种状态。没有凭据时是
   ``UNKNOWN``；收到确定性的「不支持」响应才是 ``UNSUPPORTED``；只有真实调用
   成功过才是 ``SUPPORTED``。
2. **绝不伪造状态**：本模块只负责「把命令发出去并如实返回结果」，绝不修改
   Virtual Player 的播放状态 / 曲目。状态只能由 AirPlay 的实际事件确认
   （见 :mod:`air2dlna.virtual_player`）。
3. 每个网络调用都有超时。

DACP 凭据来源（shairport-sync 元数据管道）
------------------------------------------

====================  ==========================================
``daid``              DACP-ID
``acre``              Active-Remote token
``dapo``              远程控制端口
====================  ==========================================

**现实情况（务必不要误读）**：``dapo`` 来自 shairport-sync 自己的 DACP mDNS
监控，即要求**发送端**在局域网广播 ``_dacp._tcp``；``acre``/``daid`` 来自 RTSP
头 ``Active-Remote`` / ``DACP-ID``。Apple 的 AirPlay 2 发送端（iPhone/iPad/Mac）
实际上不提供这些字段，本项目真机日志里也从未出现过。

shairport-sync ``AIRPLAY2.md`` 第 54 行写得很清楚::

    Remote control facilities are not implemented.

因此在本环境下五项能力**通常全部是 UNKNOWN**。这是**正确**结果：架构支持反向
控制、如实检测可用性、并且绝不假装状态已改变。README / CHANGELOG 不得声称
「反向控制已可用」。
"""

from __future__ import annotations

import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

log = logging.getLogger("airplay_remote")

#: 能力状态
SUPPORTED = "SUPPORTED"
UNSUPPORTED = "UNSUPPORTED"
UNKNOWN = "UNKNOWN"

#: 五项独立能力（ARCHITECTURE_V2 第 20 节）
CAPABILITIES = ("play", "pause", "seek", "next", "previous")

#: DACP 命令路径（``/ctrl-int/1/<command>``）。
#: 说明：这些路径来自 DACP 协议的公开实现（AirConnect / shairport-sync 生态、
#: Apple Remote 逆向资料）。**未在真机 AirPlay 2 发送端上验证过** —— 因为发送端
#: 不提供凭据，任何调用都会在能力检测阶段被判定为 UNKNOWN 并跳过。
COMMAND_PATHS: dict[str, str] = {
    "play": "/ctrl-int/1/play",
    "pause": "/ctrl-int/1/pause",
    "next": "/ctrl-int/1/nextitem",
    "previous": "/ctrl-int/1/previtem",
    # seek：DACP 用 setproperty 设置 dacp.playingtime（毫秒）
    "seek": "/ctrl-int/1/setproperty?dacp.playingtime=%d",
}

#: 确定「不支持」的 HTTP 状态码（服务端明确拒绝该动作）
_UNSUPPORTED_STATUS = frozenset({400, 403, 404, 405, 501})

_PORT_RE = re.compile(r"(\d{1,5})")


@dataclass
class RemoteCredentials:
    """一次 AirPlay 会话的 DACP 凭据。"""

    dacp_id: str = ""        # daid
    active_remote: str = ""  # acre
    remote_port: int = 0     # dapo
    sender_ip: str = ""

    def complete(self) -> bool:
        """凭据是否足以发起一次 DACP 调用。"""
        return bool(self.dacp_id and self.active_remote
                    and self.remote_port and self.sender_ip)

    def to_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete(),
            "has_dacp_id": bool(self.dacp_id),
            "has_active_remote": bool(self.active_remote),
            "remote_port": self.remote_port,
            "sender_ip": self.sender_ip,
        }


@dataclass
class RemoteResult:
    """一次反向控制调用的**如实**结果。"""

    ok: bool
    capability: str
    status: str = UNKNOWN     # 调用后该项能力的状态
    detail: str = ""
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "capability": self.capability,
            "status": self.status,
            "detail": self.detail,
            "path": self.path,
        }


#: 传输函数签名：``(url, headers, timeout) -> (status_code, body)``。
#: 默认实现用标准库 urllib；测试可注入本地假 DACP 服务器 / 假传输。
Transport = Callable[[str, dict[str, str], float], tuple[int, bytes]]


class AirPlayRemoteController:
    """DACP 反向控制器。

    只做三件事：保存凭据、按能力发命令、如实记录能力状态。
    **不**维护播放状态（那是 Virtual Player 的职责）。
    """

    def __init__(self, log: Optional[Callable[..., None]] = None, timeout: float = 2.0,
                 transport: Optional[Transport] = None) -> None:
        self._log = log
        self.timeout = max(0.2, float(timeout))
        self._credentials = RemoteCredentials()
        self._transport: Transport = transport or _urllib_transport
        #: 每项能力的实测状态；初始一律 UNKNOWN（不猜测）
        self._capabilities: dict[str, str] = {name: UNKNOWN for name in CAPABILITIES}
        self._last_attempt_at = 0.0
        self._last_error = ""

    # ------------------------------------------------------------------ 凭据
    @property
    def credentials(self) -> RemoteCredentials:
        return self._credentials

    def update_from_session(self, session: Optional[dict[str, Any]]) -> bool:
        """从 Virtual Player 的 ``airplay_session`` 字典刷新凭据。

        返回凭据是否发生变化（用于日志）。
        """
        session = session or {}
        port = _parse_port(session.get("remote_port"))
        sender_ip = str(session.get("client_ip") or session.get("sender_ip") or "").strip()
        new = RemoteCredentials(
            dacp_id=str(session.get("dacp_id") or "").strip(),
            active_remote=str(session.get("active_remote") or "").strip(),
            remote_port=port,
            sender_ip=sender_ip,
        )
        if new == self._credentials:
            return False
        self._credentials = new
        # 凭据变化后重新从「未知」开始判断：不要沿用上一次会话的结论
        self._capabilities = {name: UNKNOWN for name in CAPABILITIES}
        self._last_error = ""
        log.info(
            "AirPlay 反向控制凭据%s: dacp_id=%s active_remote=%s remote_port=%s sender=%s",
            "已就绪" if new.complete() else "不完整",
            "有" if new.dacp_id else "无",
            "有" if new.active_remote else "无",
            port or "无", sender_ip or "无",
        )
        if not new.complete():
            log.info(
                "AirPlay 反向控制不可用：发送端未提供 daid/acre/dapo"
                "（AirPlay 2 的 iPhone/iPad/Mac 实测不提供；shairport-sync AIRPLAY2.md "
                "明确写有 “Remote control facilities are not implemented.”）"
            )
        return True

    def clear(self) -> None:
        self.update_from_session({})

    # ------------------------------------------------------------ 能力检测
    def capability(self, name: str) -> str:
        """返回单项能力的当前状态。"""
        return self._capabilities.get(name, UNKNOWN)

    def capabilities(self) -> dict[str, str]:
        return dict(self._capabilities)

    @property
    def available(self) -> bool:
        """凭据是否完整（不代表任何能力已确认可用）。"""
        return self._credentials.complete()

    def can_issue(self, name: str) -> bool:
        """是否值得为该能力发起一次尝试。

        ``UNSUPPORTED`` 不再重试；``UNKNOWN`` 允许尝试一次以**如实探测**；
        ``SUPPORTED`` 直接允许。凭据不完整时一律不允许（避免无意义请求）。
        """
        return self.available and self.capability(name) != UNSUPPORTED

    def _note(self, capability: str, status: str, detail: str = "") -> None:
        previous = self._capabilities.get(capability, UNKNOWN)
        self._capabilities[capability] = status
        if status != previous:
            log.info("AirPlay 反向控制能力更新: %s -> %s%s",
                     capability, status, f"（{detail}）" if detail else "")

    # ------------------------------------------------------------ 命令下发
    def play(self) -> RemoteResult:
        return self._command("play", COMMAND_PATHS["play"])

    def pause(self) -> RemoteResult:
        return self._command("pause", COMMAND_PATHS["pause"])

    def next(self) -> RemoteResult:
        return self._command("next", COMMAND_PATHS["next"])

    def previous(self) -> RemoteResult:
        return self._command("previous", COMMAND_PATHS["previous"])

    def seek(self, position_ms: int) -> RemoteResult:
        try:
            target = max(0, int(position_ms))
        except (TypeError, ValueError):
            return RemoteResult(False, "seek", self.capability("seek"),
                                "seek 目标位置非法", "")
        return self._command("seek", COMMAND_PATHS["seek"] % target)

    def _command(self, capability: str, path: str) -> RemoteResult:
        """发起一次 DACP 命令，并把能力状态更新为**实测**结果。"""
        creds = self._credentials
        if not creds.complete():
            detail = ("缺少 DACP 凭据（daid/acre/dapo）；AirPlay 2 发送端实测不提供，"
                      "无法反向控制")
            return RemoteResult(False, capability, self.capability(capability), detail, path)
        if self.capability(capability) == UNSUPPORTED:
            return RemoteResult(False, capability, UNSUPPORTED,
                                "该能力此前已被实测判定为不支持", path)
        url = f"http://{creds.sender_ip}:{creds.remote_port}{path}"
        headers = {
            "Active-Remote": creds.active_remote,
            "DACP-ID": creds.dacp_id,
            # 明确标识自己，不冒充 Apple 客户端
            "User-Agent": "Air2DLNA/1.0 (DACP reverse control)",
            "Connection": "close",
        }
        self._last_attempt_at = time.monotonic()
        try:
            status, _body = self._transport(url, headers, self.timeout)
        except Exception as exc:  # noqa: BLE001 - 网络异常只记录，不伪造成功
            self._last_error = f"{type(exc).__name__}: {exc}"
            log.info("AirPlay 反向控制 %s 调用失败（能力保持 %s）: %s",
                     capability, self.capability(capability), self._last_error)
            return RemoteResult(False, capability, self.capability(capability),
                                self._last_error, path)
        if 200 <= status < 300:
            self._note(capability, SUPPORTED, f"HTTP {status}")
            return RemoteResult(True, capability, SUPPORTED, f"HTTP {status}", path)
        if status in _UNSUPPORTED_STATUS:
            self._note(capability, UNSUPPORTED, f"HTTP {status}")
            return RemoteResult(False, capability, UNSUPPORTED, f"HTTP {status}", path)
        # 其它状态码不能得出结论：保持原状态（通常是 UNKNOWN）
        self._last_error = f"HTTP {status}"
        log.info("AirPlay 反向控制 %s 返回 HTTP %s，无法据此判定能力（保持 %s）",
                 capability, status, self.capability(capability))
        return RemoteResult(False, capability, self.capability(capability),
                            self._last_error, path)

    # ------------------------------------------------------------------ 诊断
    def to_dict(self) -> dict[str, Any]:
        return {
            "credentials": self._credentials.to_dict(),
            "capabilities": self.capabilities(),
            "timeout_seconds": self.timeout,
            "last_error": self._last_error,
        }


def _parse_port(value: Any) -> int:
    """把 ``dapo`` 载荷解析为端口（容错：可能是纯数字或带前缀的文本）。"""
    if value is None:
        return 0
    if isinstance(value, int):
        return value if 0 < value < 65536 else 0
    text = str(value).strip()
    if not text:
        return 0
    match = _PORT_RE.search(text)
    if not match:
        return 0
    try:
        port = int(match.group(1))
    except ValueError:
        return 0
    return port if 0 < port < 65536 else 0


def _urllib_transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    """默认传输：标准库 urllib，强制超时。"""
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status), response.read(4096)
    except urllib.error.HTTPError as exc:
        return int(exc.code), b""
