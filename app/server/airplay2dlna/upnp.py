"""UPnP 设备描述解析、SOAP 控制与 GENA 订阅模块。

本模块仅依赖 Python 标准库，提供：

* :func:`fetch_device`：下载并解析 UPnP 设备描述 XML，返回 :class:`DeviceInfo`；
* :class:`UpnpClient`：单个 Renderer 的 SOAP 控制与 GENA 事件订阅封装；
* 若干纯函数工具：UPnP 时间解析/格式化、protocolInfo 解析与流格式选择。

设计约束：
    * 所有网络调用都设置超时；
    * :func:`fetch_device` 与 :func:`discover` 一样绝不抛出异常；
    * :class:`UpnpClient` 可被多个线程共享，底层 HTTP 请求由 ``threading.Lock`` 串行化；
    * SOAP 传输失败与 SOAP Fault 统一抛出 :class:`UpnpError`。
"""

from __future__ import annotations

import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable
from xml.sax.saxutils import escape

#: AVTransport 服务 URN（标准键，:1 与 :2 都映射到它）。
AVTRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
#: RenderingControl 服务 URN（标准键）。
RENDERINGCONTROL = "urn:schemas-upnp-org:service:RenderingControl:1"
#: ConnectionManager 服务 URN（标准键）。
CONNECTIONMANAGER = "urn:schemas-upnp-org:service:ConnectionManager:1"

#: HTTP 请求超时默认值（秒）。
DEFAULT_TIMEOUT = 5.0
#: 读取响应体的上限（防御超大/恶意响应）。
MAX_BODY_BYTES = 1024 * 1024
#: SOAP 信封使用的命名空间。
SOAP_ENVELOPE_NS = "http://schemas.xmlsoap.org/soap/envelope/"
#: SOAP 编码风格命名空间。
SOAP_ENCODING_NS = "http://schemas.xmlsoap.org/soap/encoding/"
#: 请求 User-Agent（部分设备按此做过滤）。
USER_AGENT = "airplay2dlna/0.1 UPnP/1.0"

#: 日志回调类型别名：接收单条字符串消息，可为 None。
LogFunc = Callable[[str], None] | None

#: serviceType 前缀（小写）到标准 URN 常量的映射。
_SERVICE_TYPE_MAP: dict[str, str] = {
    "urn:schemas-upnp-org:service:avtransport:": AVTRANSPORT,
    "urn:schemas-upnp-org:service:renderingcontrol:": RENDERINGCONTROL,
    "urn:schemas-upnp-org:service:connectionmanager:": CONNECTIONMANAGER,
}


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class UpnpError(Exception):
    """UPnP SOAP 故障或传输失败时抛出。"""

    def __init__(self, message: str, code: int | None = None, description: str = ""):
        """构造异常。

        :param message: 人类可读的错误描述。
        :param code: UPnPError/errorCode 数值；无则为 ``None``。
        :param description: UPnPError/errorDescription 或 faultstring。
        """
        super().__init__(message)
        self.message = message
        self.code = code
        self.description = description

    def __str__(self) -> str:
        """返回包含错误码与描述的字符串表示。"""
        parts = [self.message]
        if self.code is not None:
            parts.append(f"code={self.code}")
        if self.description:
            parts.append(f"description={self.description}")
        return " (" + ", ".join(parts[1:]) + ")" if len(parts) > 1 else self.message


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Service:
    """设备描述中的一个 UPnP 服务。"""

    #: 服务类型 URN（原始文本）。
    service_type: str
    #: 服务 ID，例如 ``urn:upnp-org:serviceId:AVTransport``。
    service_id: str
    #: 绝对控制 URL。
    control_url: str
    #: 绝对事件订阅 URL。
    event_sub_url: str
    #: 绝对 SCPD 描述 URL。
    scpd_url: str


@dataclass
class DeviceInfo:
    """一个 MediaRenderer 设备的关键信息。"""

    #: 设备 UDN，例如 ``uuid:...``。
    udn: str
    #: 友好名称。
    friendly_name: str
    #: 厂商。
    manufacturer: str
    #: 型号。
    model_name: str
    #: 设备类型，例如 ``urn:schemas-upnp-org:device:MediaRenderer:1``。
    device_type: str
    #: 设备 IP 地址（来自 SSDP 响应源地址）。
    ip: str
    #: 设备描述 XML 的 LOCATION URL。
    location: str
    #: 服务映射，键为标准 URN 常量（见本模块常量）。
    services: dict[str, Service] = field(default_factory=dict)

    def has(self, service_urn: str) -> bool:
        """判断设备是否声明了指定服务。

        :param service_urn: 标准服务 URN 常量。
        :return: 存在该服务时为 ``True``。
        """
        return service_urn in self.services


# ---------------------------------------------------------------------------
# 纯函数工具
# ---------------------------------------------------------------------------


def parse_upnp_time(value: str) -> int | None:
    """解析 UPnP 时间字符串为毫秒。

    支持 ``'H:MM:SS'``、``'HH:MM:SS'``、``'HH:MM:SS.mmm'``；
    ``'NOT_IMPLEMENTED'`` 或空串返回 ``None``；无法解析也返回 ``None``。

    :param value: UPnP 时间字符串（如 ``'0:01:23.500'``）。
    :return: 毫秒数，或 ``None``。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.upper().replace("-", "_") == "NOT_IMPLEMENTED":
        return None

    parts = text.split(":")
    if len(parts) != 3:
        return None
    try:
        hours = int(parts[0].strip() or "0")
        minutes = int(parts[1].strip() or "0")
        seconds = float(parts[2].strip() or "0")
    except (TypeError, ValueError):
        return None
    if hours < 0 or minutes < 0 or seconds < 0:
        return None
    try:
        total_ms = int(round((hours * 3600 + minutes * 60 + seconds) * 1000))
    except (OverflowError, ValueError):
        return None
    return total_ms


def format_upnp_time(seconds: int) -> str:
    """把秒数格式化为 ``'HH:MM:SS'``。

    小时位不截断（超过 99 小时时按实际位数输出），负数按 0 处理。

    :param seconds: 秒数。
    :return: 形如 ``'01:02:03'`` 的字符串。
    """
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        total = 0
    if total < 0:
        total = 0
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def parse_protocol_info(sink: str) -> list[str]:
    """按逗号切分 ConnectionManager 的 Sink/Source protocolInfo 字符串。

    :param sink: 原始 protocolInfo 文本（逗号分隔）。
    :return: 去空白、丢弃空项后的条目列表。
    """
    if not sink:
        return []
    try:
        text = str(sink)
    except Exception:  # noqa: BLE001 - 极端对象也不应抛出
        return []
    return [entry.strip() for entry in text.split(",") if entry.strip()]


def pick_stream_mime(sink_entries: list[str]) -> tuple[str, str] | None:
    """为实时 16bit/44100Hz/立体声 LPCM 流选择最佳 Sink 格式。

    每个条目形如 ``'http-get:*:audio/wav:*'`` 或
    ``'http-get:*:audio/L16;rate=44100;channels=2:*'``。

    偏好顺序（大小写不敏感，按 MIME 部分匹配）：

    1. ``audio/wav`` / ``audio/x-wav`` / ``audio/wave`` -> ``('audio/wav', 'wav')``
    2. ``audio/l16``（接受任意 rate/channels 参数）
       -> ``('audio/L16;rate=44100;channels=2', 'l16')``
    3. ``audio/l8`` / ``audio/l24`` / ``audio/basic`` -> ``('<原始 mime>', 'l16')``

    仅考虑协议部分以 ``http-get`` 开头的条目。

    :param sink_entries: Sink protocolInfo 条目列表。
    :return: ``(mime, 编码标签)``，无匹配时返回 ``None``。
    """
    if not sink_entries:
        return None

    wav_match: tuple[str, str] | None = None
    l16_match: tuple[str, str] | None = None
    fallback_match: tuple[str, str] | None = None

    for entry in sink_entries:
        if not entry:
            continue
        text = str(entry).strip()
        if not text:
            continue
        parts = text.split(":")
        if len(parts) < 3:
            continue
        if not parts[0].strip().lower().startswith("http-get"):
            continue
        mime = parts[2].strip()
        if not mime:
            continue
        base = mime.split(";", 1)[0].strip().lower()

        if base in ("audio/wav", "audio/x-wav", "audio/wave"):
            if wav_match is None:
                wav_match = ("audio/wav", "wav")
        elif base == "audio/l16":
            if l16_match is None:
                l16_match = ("audio/L16;rate=44100;channels=2", "l16")
        elif base in ("audio/l8", "audio/l24", "audio/basic"):
            if fallback_match is None:
                fallback_match = (mime, "l16")

    return wav_match or l16_match or fallback_match


# ---------------------------------------------------------------------------
# XML 工具
# ---------------------------------------------------------------------------


def _log(log: LogFunc, message: str) -> None:
    """安全地调用日志回调：回调缺失或自身抛错都不影响主流程。"""
    if log is None:
        return
    try:
        log(str(message))
    except Exception:  # noqa: BLE001
        pass


def _local_name(tag: object) -> str:
    """去掉 XML 命名空间前缀，返回标签本地名。"""
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{"):
        _, _, local = tag.partition("}")
        return local
    return tag


def _find_first(element: ET.Element, local_name: str) -> ET.Element | None:
    """在子树中按本地名查找第一个匹配元素（先序遍历）。"""
    if _local_name(element.tag) == local_name:
        return element
    for child in element.iter():
        if _local_name(child.tag) == local_name:
            return child
    return None


def _child_text(element: ET.Element, local_name: str) -> str:
    """读取直接子元素的文本（去空白），不存在时返回空串。"""
    for child in list(element):
        if _local_name(child.tag) == local_name:
            return (child.text or "").strip()
    return ""


def _resolve(base: str, url: str) -> str:
    """把可能为相对的 URL 解析为绝对 URL。"""
    if not url:
        return ""
    try:
        return urllib.parse.urljoin(base, url.strip())
    except Exception:  # noqa: BLE001
        return url.strip()


def _service_key(service_type: str) -> str:
    """把 serviceType 映射为标准 URN 常量键，未知类型原样返回。"""
    text = (service_type or "").strip()
    lowered = text.lower()
    for prefix, urn in _SERVICE_TYPE_MAP.items():
        if lowered.startswith(prefix):
            return urn
    return text


def parse_device_description(
    xml_text: str, location: str, ip: str = ""
) -> DeviceInfo | None:
    """解析 UPnP 设备描述 XML，返回首个 MediaRenderer 的 :class:`DeviceInfo`。

    规则：

    * 在整棵树（含 ``deviceList`` 递归）中查找第一个 ``deviceType`` 含
      ``'MediaRenderer'`` 的 ``device``；找不到返回 ``None``；
    * ``friendlyName`` / ``UDN`` / ``manufacturer`` / ``modelName`` 容错读取，
      缺失时为空串；
    * 收集该设备（含其内嵌设备）中的全部 ``service``；
    * ``controlURL`` / ``eventSubURL`` / ``SCPDURL`` 用 ``urljoin`` 相对
      ``location`` 解析为绝对 URL；
    * 忽略 XML 命名空间（剥离 ``{...}``）。

    :param xml_text: 设备描述 XML 文本。
    :param location: 该描述的 LOCATION URL（作为相对 URL 基准）。
    :param ip: 设备 IP（来自 SSDP 源地址）。
    :return: 解析结果；XML 非法或无 MediaRenderer 时为 ``None``。
    """
    if not xml_text or not str(xml_text).strip():
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    chosen: ET.Element | None = None
    for element in root.iter():
        if _local_name(element.tag) != "device":
            continue
        device_type = _child_text(element, "deviceType")
        if "mediarenderer" in device_type.lower():
            chosen = element
            break
    if chosen is None:
        return None

    services: dict[str, Service] = {}
    for service_el in chosen.iter():
        if _local_name(service_el.tag) != "service":
            continue
        service_type = _child_text(service_el, "serviceType")
        service = Service(
            service_type=service_type,
            service_id=_child_text(service_el, "serviceId"),
            control_url=_resolve(location, _child_text(service_el, "controlURL")),
            event_sub_url=_resolve(location, _child_text(service_el, "eventSubURL")),
            scpd_url=_resolve(location, _child_text(service_el, "SCPDURL")),
        )
        key = _service_key(service_type)
        # 同一标准键出现多次时保留首个（通常为主设备的声明）。
        if key and key not in services:
            services[key] = service

    return DeviceInfo(
        udn=_child_text(chosen, "UDN"),
        friendly_name=_child_text(chosen, "friendlyName"),
        manufacturer=_child_text(chosen, "manufacturer"),
        model_name=_child_text(chosen, "modelName"),
        device_type=_child_text(chosen, "deviceType"),
        ip=ip or "",
        location=location or "",
        services=services,
    )


def fetch_device(
    location: str,
    ip: str,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 2,
    log: LogFunc = None,
) -> DeviceInfo | None:
    """下载并解析设备描述，返回 :class:`DeviceInfo`。

    会重试 ``retries`` 次（因此最多尝试 ``retries + 1`` 次）。
    任何网络或解析错误都只记录日志并返回 ``None``，绝不抛出异常。

    :param location: 设备描述 LOCATION URL（绝对 http/https）。
    :param ip: 设备 IP 地址。
    :param timeout: 单次请求超时秒数。
    :param retries: 失败后的重试次数（不含首次尝试）。
    :param log: 日志回调。
    :return: 解析结果，失败为 ``None``。
    """
    if not location or not isinstance(location, str):
        _log(log, f"upnp: fetch_device called with empty location (ip={ip!r})")
        return None

    try:
        scheme = urllib.parse.urlsplit(location).scheme.lower()
    except Exception:  # noqa: BLE001
        scheme = ""
    if scheme not in ("http", "https"):
        _log(log, f"upnp: unsupported LOCATION scheme {scheme!r} for {location!r}")
        return None

    try:
        attempts = max(1, int(retries) + 1)
    except (TypeError, ValueError):
        attempts = 1
    try:
        request_timeout = max(0.001, float(timeout))
    except (TypeError, ValueError):
        request_timeout = DEFAULT_TIMEOUT

    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                location,
                headers={"User-Agent": USER_AGENT, "Connection": "close"},
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                status = int(getattr(response, "status", 200) or 200)
                if status >= 400:
                    raise UpnpError(f"HTTP {status} while fetching device description")
                raw = response.read(MAX_BODY_BYTES)
            xml_text = raw.decode("utf-8", "replace")
            info = parse_device_description(xml_text, location, ip)
            if info is None:
                _log(log, f"upnp: no MediaRenderer found in description at {location}")
                return None
            _log(
                log,
                f"upnp: resolved device {info.friendly_name!r} ({info.udn}) at {ip}",
            )
            return info
        except Exception as exc:  # noqa: BLE001 - 网络/解析错误一律降级
            last_error = exc
            _log(
                log,
                f"upnp: fetch_device attempt {attempt + 1}/{attempts} "
                f"failed for {location}: {exc}",
            )

    _log(log, f"upnp: fetch_device giving up for {location}: {last_error}")
    return None


# ---------------------------------------------------------------------------
# SOAP 构造与解析
# ---------------------------------------------------------------------------


def build_soap_envelope(service_urn: str, action: str, args: dict[str, str]) -> str:
    """构造 SOAP 请求体（UTF-8 文本，XML 转义所有参数值）。

    :param service_urn: 目标服务 URN。
    :param action: 动作名，例如 ``'GetTransportInfo'``。
    :param args: 入参名 -> 值。
    :return: 完整的 SOAP Envelope XML 文本。
    """
    safe_urn = escape(str(service_urn or ""))
    safe_action = escape(str(action or ""))
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<s:Envelope xmlns:s="{SOAP_ENVELOPE_NS}"',
        f'            s:encodingStyle="{SOAP_ENCODING_NS}">',
        "  <s:Body>",
        f'    <u:{safe_action} xmlns:u="{safe_urn}">',
    ]
    for name, value in (args or {}).items():
        safe_name = escape(str(name))
        safe_value = escape("" if value is None else str(value))
        lines.append(f"      <{safe_name}>{safe_value}</{safe_name}>")
    lines.append(f"    </u:{safe_action}>")
    lines.append("  </s:Body>")
    lines.append("</s:Envelope>")
    return "\n".join(lines)


def parse_soap_response(data: bytes | str) -> dict[str, str]:
    """解析 SOAP 响应，返回动作出参 ``{标签名: 文本}``。

    遇到 SOAP Fault 时抛出 :class:`UpnpError`，其中 ``code`` 来自
    ``UPnPError/errorCode``，``description`` 来自 ``errorDescription``
    或 ``faultstring``。

    :param data: 响应体（bytes 或 str）。
    :return: 出参字典；响应体无 ``*Response`` 元素时返回 ``{}``。
    :raises UpnpError: XML 非法或响应为 SOAP Fault。
    """
    if isinstance(data, (bytes, bytearray)):
        text = bytes(data).decode("utf-8", "replace")
    elif data is None:
        text = ""
    else:
        text = str(data)

    if not text.strip():
        raise UpnpError("empty SOAP response")

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise UpnpError(f"invalid SOAP XML: {exc}") from exc

    fault = _find_first(root, "Fault")
    if fault is not None:
        code: int | None = None
        description = ""
        code_el = _find_first(fault, "errorCode")
        if code_el is not None and (code_el.text or "").strip():
            try:
                code = int((code_el.text or "").strip())
            except ValueError:
                code = None
        desc_el = _find_first(fault, "errorDescription")
        if desc_el is not None and (desc_el.text or "").strip():
            description = (desc_el.text or "").strip()
        if not description:
            string_el = _find_first(fault, "faultstring")
            if string_el is not None and (string_el.text or "").strip():
                description = (string_el.text or "").strip()
        if code is not None:
            message = f"UPnP SOAP fault {code}: {description or 'unknown error'}"
        else:
            message = f"SOAP fault: {description or 'unknown error'}"
        raise UpnpError(message, code=code, description=description)

    response_el: ET.Element | None = None
    for element in root.iter():
        if _local_name(element.tag).endswith("Response"):
            response_el = element
            break
    if response_el is None:
        return {}

    result: dict[str, str] = {}
    for child in list(response_el):
        name = _local_name(child.tag)
        if not name:
            continue
        result[name] = (child.text or "").strip()
    return result


# ---------------------------------------------------------------------------
# UPnP 客户端
# ---------------------------------------------------------------------------


class UpnpClient:
    """单个 UPnP 设备的控制客户端（线程安全）。

    :class:`UpnpClient` 可被多个线程共享：所有底层 HTTP 调用都由
    实例内部的 :class:`threading.Lock` 串行化。
    """

    def __init__(self, device: DeviceInfo, timeout: float = DEFAULT_TIMEOUT, log: LogFunc = None):
        """初始化客户端。

        :param device: 已解析的设备信息。
        :param timeout: 所有 HTTP 请求的超时秒数。
        :param log: 日志回调。
        """
        self.device = device
        self.log = log
        try:
            self.timeout = max(0.001, float(timeout))
        except (TypeError, ValueError):
            self.timeout = DEFAULT_TIMEOUT
        self._lock = threading.Lock()

    # -- 底层 HTTP ---------------------------------------------------------

    def _http(
        self, request: urllib.request.Request
    ) -> tuple[int, dict[str, str], bytes]:
        """执行一次 HTTP 请求（持锁）。

        :param request: 已构造好的请求。
        :return: ``(状态码, 响应头, 响应体)``；HTTP 错误也作为正常返回值
            （状态码 >= 400），以便 SOAP Fault 解析。
        :raises UpnpError: 连接失败、超时等传输层错误。
        """
        with self._lock:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    status = int(getattr(response, "status", 200) or 200)
                    headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
                    body = response.read(MAX_BODY_BYTES)
                    return status, headers, body
            except urllib.error.HTTPError as exc:
                headers = {}
                try:
                    headers = {str(k).lower(): str(v) for k, v in (exc.headers or {}).items()}
                except Exception:  # noqa: BLE001
                    headers = {}
                body = b""
                try:
                    body = exc.read(MAX_BODY_BYTES)
                except Exception:  # noqa: BLE001
                    body = b""
                return int(exc.code or 0), headers, body
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", exc)
                if isinstance(reason, (socket.timeout, TimeoutError)):
                    raise UpnpError(f"timeout after {self.timeout}s: {reason}") from exc
                raise UpnpError(f"connection error: {reason}") from exc
            except (socket.timeout, TimeoutError) as exc:
                raise UpnpError(f"timeout after {self.timeout}s") from exc
            except OSError as exc:
                raise UpnpError(f"network error: {exc}") from exc

    # -- SOAP --------------------------------------------------------------

    def soap(self, service_urn: str, action: str, args: dict[str, str]) -> dict[str, str]:
        """执行一个 SOAP 动作并返回出参字典。

        请求头固定为::

            Content-Type: text/xml; charset="utf-8"
            SOAPACTION: "<service_urn>#<action>"
            Connection: close

        :param service_urn: 服务 URN（本模块常量）。
        :param action: 动作名。
        :param args: 入参名 -> 值（值会被 XML 转义）。
        :return: 出参 ``{标签名: 文本}``。
        :raises UpnpError: 设备未提供该服务、传输失败或响应为 SOAP Fault。
        """
        service = self.device.services.get(service_urn)
        if service is None:
            raise UpnpError(f"device does not expose service {service_urn}")
        if not service.control_url:
            raise UpnpError(f"service {service_urn} has no controlURL")

        body = build_soap_envelope(service_urn, action, args).encode("utf-8")
        request = urllib.request.Request(
            service.control_url,
            data=body,
            headers={
                "Content-Type": 'text/xml; charset="utf-8"',
                "SOAPACTION": f'"{service_urn}#{action}"',
                "Connection": "close",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )

        status, _headers, payload = self._http(request)
        if status >= 400:
            # 典型情况：HTTP 500 + SOAP Fault 体；若能解析出 Fault 则抛出带码错误，
            # 否则退化为普通 HTTP 错误。
            try:
                parsed = parse_soap_response(payload)
            except UpnpError as exc:
                if exc.code is not None or exc.description:
                    raise
                raise UpnpError(
                    f"HTTP {status} from {service.control_url}: {exc.message}"
                ) from exc
            del parsed
            raise UpnpError(f"HTTP {status} from {service.control_url}")

        result = parse_soap_response(payload)
        _log(self.log, f"upnp: {action} on {service_urn.split(':')[-2]} ok")
        return result

    # -- 便捷封装（InstanceID 固定为 0） ------------------------------------

    def get_transport_info(self) -> dict:
        """调用 ``GetTransportInfo``。

        :return: ``{'state': str, 'status': str, 'speed': str}``。
        """
        out = self.soap(AVTRANSPORT, "GetTransportInfo", {"InstanceID": "0"})
        return {
            "state": out.get("CurrentTransportState", ""),
            "status": out.get("CurrentTransportStatus", ""),
            "speed": out.get("CurrentSpeed", ""),
        }

    def get_position_info(self) -> dict:
        """调用 ``GetPositionInfo``。

        :return: 含 ``rel_time_ms`` / ``duration_ms`` / ``track_uri`` /
            ``track_metadata`` / ``track`` 的字典。
        """
        out = self.soap(AVTRANSPORT, "GetPositionInfo", {"InstanceID": "0"})
        try:
            track = int(out.get("Track", "0") or "0")
        except (TypeError, ValueError):
            track = 0
        return {
            "rel_time_ms": parse_upnp_time(out.get("RelTime", "")),
            "duration_ms": parse_upnp_time(out.get("TrackDuration", "")),
            "track_uri": out.get("TrackURI", ""),
            "track_metadata": out.get("TrackMetaData", ""),
            "track": track,
        }

    def get_media_info(self) -> dict:
        """调用 ``GetMediaInfo``。

        :return: 含 ``nr_tracks`` / ``duration_ms`` / ``current_uri`` /
            ``current_uri_metadata`` 的字典。
        """
        out = self.soap(AVTRANSPORT, "GetMediaInfo", {"InstanceID": "0"})
        try:
            nr_tracks = int(out.get("NrTracks", "0") or "0")
        except (TypeError, ValueError):
            nr_tracks = 0
        return {
            "nr_tracks": nr_tracks,
            "duration_ms": parse_upnp_time(out.get("MediaDuration", "")),
            "current_uri": out.get("CurrentURI", ""),
            "current_uri_metadata": out.get("CurrentURIMetaData", ""),
        }

    def get_volume(self) -> int:
        """读取 RenderingControl 的 Master 音量。

        :return: 0..100 的整数（越界值会被裁剪）。
        """
        out = self.soap(
            RENDERINGCONTROL,
            "GetVolume",
            {"InstanceID": "0", "Channel": "Master"},
        )
        try:
            volume = int(out.get("CurrentVolume", "0") or "0")
        except (TypeError, ValueError):
            volume = 0
        return max(0, min(100, volume))

    def set_volume(self, volume: int) -> None:
        """设置 RenderingControl 的 Master 音量。

        :param volume: 0..100；越界会被裁剪。
        """
        try:
            value = int(volume)
        except (TypeError, ValueError):
            value = 0
        value = max(0, min(100, value))
        self.soap(
            RENDERINGCONTROL,
            "SetVolume",
            {"InstanceID": "0", "Channel": "Master", "DesiredVolume": str(value)},
        )

    def get_mute(self) -> bool:
        """读取 Master 静音状态。

        :return: 静音返回 ``True``。
        """
        out = self.soap(
            RENDERINGCONTROL,
            "GetMute",
            {"InstanceID": "0", "Channel": "Master"},
        )
        return _as_bool(out.get("CurrentMute", ""))

    def set_mute(self, mute: bool) -> None:
        """设置 Master 静音状态。

        :param mute: ``True`` 静音，``False`` 取消静音。
        """
        self.soap(
            RENDERINGCONTROL,
            "SetMute",
            {
                "InstanceID": "0",
                "Channel": "Master",
                "DesiredMute": "1" if mute else "0",
            },
        )

    def get_protocol_info(self) -> tuple[list[str], list[str]]:
        """调用 ConnectionManager 的 ``GetProtocolInfo``。

        :return: ``(source_list, sink_list)``，元素为原始 protocolInfo 字符串。
        """
        out = self.soap(CONNECTIONMANAGER, "GetProtocolInfo", {})
        return (
            parse_protocol_info(out.get("Source", "")),
            parse_protocol_info(out.get("Sink", "")),
        )

    def set_av_transport_uri(self, uri: str, metadata: str = "") -> None:
        """调用 ``SetAVTransportURI``。

        :param uri: 媒体 URI。
        :param metadata: DIDL-Lite 元数据（可为空）。
        """
        self.soap(
            AVTRANSPORT,
            "SetAVTransportURI",
            {
                "InstanceID": "0",
                "CurrentURI": uri,
                "CurrentURIMetaData": metadata,
            },
        )

    def play(self, speed: str = "1") -> None:
        """调用 ``Play``。

        :param speed: 播放速度，默认 ``'1'``。
        """
        self.soap(AVTRANSPORT, "Play", {"InstanceID": "0", "Speed": str(speed)})

    def pause(self) -> None:
        """调用 ``Pause``。"""
        self.soap(AVTRANSPORT, "Pause", {"InstanceID": "0"})

    def stop(self) -> None:
        """调用 ``Stop``。"""
        self.soap(AVTRANSPORT, "Stop", {"InstanceID": "0"})

    def seek_rel_time(self, target_seconds: int) -> None:
        """按相对时间跳转（``Unit='REL_TIME'``）。

        :param target_seconds: 目标秒数，格式化为 ``'HH:MM:SS'``。
        """
        self.soap(
            AVTRANSPORT,
            "Seek",
            {
                "InstanceID": "0",
                "Unit": "REL_TIME",
                "Target": format_upnp_time(target_seconds),
            },
        )

    def next(self) -> None:
        """调用 ``Next``。"""
        self.soap(AVTRANSPORT, "Next", {"InstanceID": "0"})

    def previous(self) -> None:
        """调用 ``Previous``。"""
        self.soap(AVTRANSPORT, "Previous", {"InstanceID": "0"})

    # -- GENA --------------------------------------------------------------

    def subscribe(self, callback_url: str, timeout_s: int = 1800) -> str | None:
        """对 AVTransport 的 eventSubURL 发起 GENA SUBSCRIBE。

        请求头::

            CALLBACK: <callback_url>
            NT: upnp:event
            TIMEOUT: Second-<timeout_s>

        :param callback_url: 事件回调 URL。
        :param timeout_s: 订阅时长（秒）。
        :return: 响应头 ``SID``；失败时记录日志并返回 ``None``。
        """
        service = self.device.services.get(AVTRANSPORT)
        if service is None or not service.event_sub_url:
            _log(self.log, "upnp: subscribe failed: no AVTransport eventSubURL")
            return None
        if not callback_url:
            _log(self.log, "upnp: subscribe failed: empty callback URL")
            return None

        request = urllib.request.Request(
            service.event_sub_url,
            headers={
                "CALLBACK": f"<{callback_url}>",
                "NT": "upnp:event",
                "TIMEOUT": f"Second-{int(timeout_s)}",
                "Connection": "close",
                "User-Agent": USER_AGENT,
            },
            method="SUBSCRIBE",
        )
        try:
            status, headers, _body = self._http(request)
        except UpnpError as exc:
            _log(self.log, f"upnp: subscribe transport error: {exc}")
            return None
        if status >= 400:
            _log(self.log, f"upnp: subscribe failed with HTTP {status}")
            return None
        sid = headers.get("sid", "") or ""
        if not sid:
            _log(self.log, "upnp: subscribe response had no SID header")
            return None
        _log(self.log, f"upnp: subscribed SID={sid}")
        return sid

    def renew_subscription(self, sid: str, timeout_s: int = 1800) -> bool:
        """续订已有 GENA 订阅。

        :param sid: ``subscribe`` 返回的 SID。
        :param timeout_s: 新的订阅时长（秒）。
        :return: 成功（HTTP 2xx）返回 ``True``。
        """
        service = self.device.services.get(AVTRANSPORT)
        if service is None or not service.event_sub_url or not sid:
            _log(self.log, "upnp: renew failed: missing eventSubURL or SID")
            return False
        request = urllib.request.Request(
            service.event_sub_url,
            headers={
                "SID": sid,
                "TIMEOUT": f"Second-{int(timeout_s)}",
                "Connection": "close",
                "User-Agent": USER_AGENT,
            },
            method="SUBSCRIBE",
        )
        try:
            status, _headers, _body = self._http(request)
        except UpnpError as exc:
            _log(self.log, f"upnp: renew transport error: {exc}")
            return False
        if status >= 400:
            _log(self.log, f"upnp: renew failed with HTTP {status}")
            return False
        return True

    def unsubscribe(self, sid: str) -> bool:
        """取消 GENA 订阅。

        :param sid: 要取消的 SID。
        :return: 成功（HTTP 2xx）返回 ``True``。
        """
        service = self.device.services.get(AVTRANSPORT)
        if service is None or not service.event_sub_url or not sid:
            _log(self.log, "upnp: unsubscribe failed: missing eventSubURL or SID")
            return False
        request = urllib.request.Request(
            service.event_sub_url,
            headers={
                "SID": sid,
                "Connection": "close",
                "User-Agent": USER_AGENT,
            },
            method="UNSUBSCRIBE",
        )
        try:
            status, _headers, _body = self._http(request)
        except UpnpError as exc:
            _log(self.log, f"upnp: unsubscribe transport error: {exc}")
            return False
        if status >= 400:
            _log(self.log, f"upnp: unsubscribe failed with HTTP {status}")
            return False
        return True


def _as_bool(value: str) -> bool:
    """把 UPnP 布尔文本解析为 ``bool``（兼容 0/1/true/false/yes/no/on/off）。"""
    text = (value or "").strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    try:
        return int(text) != 0
    except ValueError:
        return False
