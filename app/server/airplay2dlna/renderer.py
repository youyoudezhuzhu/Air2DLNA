"""DLNA Renderer 注册表：发现、在线判定、能力探测、用户选择。

对应 TECHNICAL_DESIGN 第 6、7、11 节。

关键约束（任务书第 7 节）：用户选定的 ``selected_renderer_udn`` 是唯一依据。
设备离线时只把状态标为 offline，**绝不自动切换到另一个设备**。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from . import netif, ssdp, upnp

log = logging.getLogger("renderer")


@dataclass
class RendererRecord:
    """一个已发现的 UPnP MediaRenderer。"""

    udn: str
    name: str
    ip: str
    location: str
    model: str = ""
    manufacturer: str = ""
    device: Optional[upnp.DeviceInfo] = None
    client: Optional[upnp.UpnpClient] = None
    last_seen: float = field(default_factory=time.monotonic)
    online: bool = True
    sink_protocols: list[str] = field(default_factory=list)
    supported_mime: Optional[str] = None
    stream_kind: Optional[str] = None  # 'wav' | 'l16'
    capabilities_checked: bool = False
    capability_error: str = ""

    def to_dict(self, selected_udn: str = "") -> dict:
        return {
            "udn": self.udn,
            "name": self.name,
            "ip": self.ip,
            "model": self.model,
            "manufacturer": self.manufacturer,
            "online": self.online,
            "supported_mime": self.supported_mime,
            "stream_kind": self.stream_kind,
            "capabilities_checked": self.capabilities_checked,
            "capability_error": self.capability_error,
            "selected": self.udn == selected_udn,
        }


class RendererRegistry:
    """线程安全的 Renderer 集合。"""

    def __init__(self, log_level: str = "info", rediscover_seconds: int = 300) -> None:
        self._lock = threading.RLock()
        self._renderers: dict[str, RendererRecord] = {}
        self._selected_udn = ""
        self._discovering = False
        self._last_scan_monotonic = 0.0
        self._rediscover_seconds = max(30, int(rediscover_seconds))
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._capability_threads: dict[str, threading.Thread] = {}

    # ------------------------------------------------------------------ 查询
    def list(self) -> list[dict]:
        with self._lock:
            records = sorted(
                self._renderers.values(),
                key=lambda r: (not r.online, r.name.lower(), r.udn),
            )
            return [r.to_dict(self._selected_udn) for r in records]

    def get(self, udn: str) -> Optional[RendererRecord]:
        with self._lock:
            return self._renderers.get(udn or "")

    @property
    def selected_udn(self) -> str:
        with self._lock:
            return self._selected_udn

    def selected(self) -> Optional[RendererRecord]:
        with self._lock:
            record = self._renderers.get(self._selected_udn)
            return record

    @property
    def discovering(self) -> bool:
        with self._lock:
            return self._discovering

    def count(self) -> tuple[int, int]:
        """返回 (总数, 在线数)。"""
        with self._lock:
            total = len(self._renderers)
            online = sum(1 for r in self._renderers.values() if r.online)
            return total, online

    # ------------------------------------------------------------------ 选择
    def set_selected(self, udn: str, name: str = "", ip: str = "") -> RendererRecord:
        """设置当前选中的 Renderer。``udn`` 必须已发现（防止持久化任意值）。"""
        with self._lock:
            if not udn:
                self._selected_udn = ""
                return RendererRecord(udn="", name="", ip="", location="")
            record = self._renderers.get(udn)
            if record is None:
                raise KeyError(f"未发现的 Renderer: {udn}")
            self._selected_udn = udn
            if name:
                record.name = record.name or name
            if ip:
                record.ip = ip
            log.info("Renderer selected udn=%s name=%s ip=%s", record.udn, record.name, record.ip)
            return record

    def restore_selection(self, udn: str, name: str = "", ip: str = "") -> None:
        """启动时恢复持久化的选择；设备尚未发现也要记住 UDN（显示为离线）。"""
        with self._lock:
            self._selected_udn = udn or ""
            if udn and udn not in self._renderers:
                self._renderers[udn] = RendererRecord(
                    udn=udn, name=name or udn, ip=ip, location="", online=False
                )
                log.info(
                    "恢复已保存的 Renderer（尚未发现，显示为离线）: %s (%s)", name or udn, udn
                )

    # ------------------------------------------------------------------ 发现
    def scan(self, timeout: float = 2.5, rounds: int = 3, deep: bool = False) -> int:
        """执行一次 SSDP 发现。返回本轮发现的设备数。"""
        with self._lock:
            if self._discovering:
                log.debug("已有发现在进行中，跳过本次")
                return 0
            self._discovering = True
        try:
            addresses = netif.select_lan_addresses()
            log.info("DLNA discovery started (interfaces=%s)", addresses)
            responses = ssdp.discover(
                timeout=timeout, rounds=rounds, interfaces=addresses, log=log.debug
            )
            found = 0
            seen_udns: set[str] = set()
            for response in responses:
                if not response.location:
                    continue
                udn = response.udn
                if not udn or udn in seen_udns:
                    continue
                seen_udns.add(udn)
                if self._upsert_from_response(udn, response):
                    found += 1
            self._mark_missing_offline(seen_udns, deep=deep)
            with self._lock:
                self._last_scan_monotonic = time.monotonic()
            log.info(
                "DLNA discovery finished: %d 台响应, %d 台 MediaRenderer",
                len(responses), found,
            )
            return found
        except Exception:  # noqa: BLE001 - 发现失败绝不能让应用崩溃
            log.exception("SSDP 发现过程发生异常")
            return 0
        finally:
            with self._lock:
                self._discovering = False

    def _upsert_from_response(self, udn: str, response: ssdp.SsdpResponse) -> bool:
        device = upnp.fetch_device(response.location, response.ip, log=log.debug)
        if device is None:
            log.debug("设备描述获取失败，忽略: %s", response.location)
            return False
        if not device.has(upnp.AVTRANSPORT):
            log.debug("设备缺少 AVTransport 服务，忽略: %s", device.friendly_name)
            return False

        with self._lock:
            record = self._renderers.get(udn)
            is_new = record is None
            if record is None:
                record = RendererRecord(
                    udn=udn,
                    name=device.friendly_name or udn,
                    ip=response.ip,
                    location=response.location,
                    device=device,
                    client=upnp.UpnpClient(device, log=log.debug),
                )
                self._renderers[udn] = record
                log.info(
                    "Renderer found udn=%s name=%r ip=%s model=%r manufacturer=%r",
                    udn, record.name, record.ip, device.model_name, device.manufacturer,
                )
            else:
                record.name = device.friendly_name or record.name
                record.ip = response.ip
                record.location = response.location
                record.model = device.model_name
                record.manufacturer = device.manufacturer
                record.device = device
                record.client = upnp.UpnpClient(device, log=log.debug)
            record.model = device.model_name
            record.manufacturer = device.manufacturer
            was_offline = not record.online
            record.online = True
            record.last_seen = time.monotonic()
            needs_probe = is_new or not record.capabilities_checked

        if was_offline:
            log.info("Renderer 重新上线: %s (%s)", record.name, record.udn)
        if needs_probe:
            self._spawn_capability_probe(record)
        return is_new

    def _mark_missing_offline(self, seen_udns: set[str], deep: bool = False) -> None:
        """把本轮未响应的设备标记为离线。

        只有 ``deep=True``（周期全量重扫）才做离线判定，避免用户按「搜索设备」时
        因单轮丢包就误判在线设备离线。
        """
        if not deep:
            return
        now = time.monotonic()
        with self._lock:
            for record in self._renderers.values():
                if record.udn in seen_udns:
                    continue
                if not record.online:
                    continue
                # 连续 2 个重扫周期都没出现才判定离线
                if now - record.last_seen > self._rediscover_seconds * 2:
                    record.online = False
                    log.warning("Renderer disconnected (udn=%s) -> offline", record.udn)

    def mark_offline(self, udn: str, reason: str = "") -> None:
        with self._lock:
            record = self._renderers.get(udn)
            if record and record.online:
                record.online = False
                log.warning("Renderer unavailable (udn=%s) %s", udn, reason)

    # -------------------------------------------------------------- 能力探测
    def _spawn_capability_probe(self, record: RendererRecord) -> None:
        with self._lock:
            existing = self._capability_threads.get(record.udn)
            if existing is not None and existing.is_alive():
                return
            thread = threading.Thread(
                target=self._probe_capabilities,
                args=(record.udn,),
                name=f"cap-{record.udn[-8:]}",
                daemon=True,
            )
            self._capability_threads[record.udn] = thread
        thread.start()

    def _probe_capabilities(self, udn: str) -> None:
        record = self.get(udn)
        if record is None or record.client is None:
            return
        try:
            _source, sink = record.client.get_protocol_info()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                record.capability_error = f"GetProtocolInfo 失败: {exc}"
                record.capabilities_checked = True
            log.warning("Renderer %s GetProtocolInfo 失败: %s", record.name, exc)
            return

        choice = upnp.pick_stream_mime(sink)
        with self._lock:
            record.sink_protocols = sink
            record.capabilities_checked = True
            if choice is None:
                record.supported_mime = None
                record.stream_kind = None
                record.capability_error = (
                    "Renderer does not support required MIME type "
                    "(需要 audio/wav 或 audio/L16)"
                )
                log.error(
                    "Renderer does not support required MIME type: name=%s sink=%s",
                    record.name, ",".join(sink) if sink else "(空)",
                )
            else:
                mime, kind = choice
                record.supported_mime = mime
                record.stream_kind = kind
                record.capability_error = ""
                log.info(
                    "Renderer %s 支持实时流格式: %s (%s)", record.name, mime, kind
                )

    def resolve_stream_kind(self, record: RendererRecord, preferred: str = "auto") -> tuple[str, str]:
        """决定实际使用的 (content_type, kind)。

        ``preferred`` 为配置项 ``output_format``：auto / wav / l16。
        设备能力优先；设备能力未知时保守使用 wav。
        """
        with self._lock:
            kind = record.stream_kind
        log.debug("流格式解析: preferred=%r device_kind=%r name=%r", preferred, kind, record.name)
        if preferred in ("wav", "l16"):
            # 用户显式指定：若设备明确不支持则仍然尊重用户（日志给出警告）
            if kind and kind != preferred:
                log.warning(
                    "用户指定 output_format=%s，但设备 %s 声明支持 %s；按用户指定执行",
                    preferred, record.name, kind,
                )
            return ("audio/wav" if preferred == "wav" else "audio/L16;rate=44100;channels=2"), preferred
        if kind == "wav":
            return "audio/wav", "wav"
        if kind == "l16":
            return "audio/L16;rate=44100;channels=2", "l16"
        # 能力未知：保守尝试 WAV
        return "audio/wav", "wav"

    # ------------------------------------------------------------ 后台重扫
    def start_background(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._background_loop, name="ssdp-rescan", daemon=True)
        self._thread.start()

    def _background_loop(self) -> None:
        # 首次启动延后一点，让主流程先把服务拉起来
        self._stop_event.wait(1.0)
        first = True
        while not self._stop_event.is_set():
            try:
                self.scan(timeout=2.5, rounds=3 if first else 2, deep=not first)
            except Exception:  # noqa: BLE001
                log.exception("后台 SSDP 重扫异常")
            first = False
            self._stop_event.wait(self._rediscover_seconds)

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
