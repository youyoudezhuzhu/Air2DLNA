"""播放状态统一管理与 AirPlay ↔ DLNA 同步。

对应 TECHNICAL_DESIGN 第 8、10、11、12、19 节。

设计原则（任务书第 10 节明确要求）：**不**把 AirPlay 指令逐条直译成 SOAP。
所有 AirPlay 事件先归一到内部 :class:`PlaybackState`，再由一个后台「收敛线程」
把渲染器的**实际状态**驱动到**目标状态**。因此：

* 短时间的 Play/Pause 抖动不会造成 SOAP 风暴；
* 渲染器暂时不可达时状态被记为「待收敛」，设备恢复后自动补齐；
* 渲染器上报的状态始终如实暴露为 ``rendererState``，UI 不会说谎。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import netif, stream, upnp
from .metadata import MetadataItem
from .ringbuffer import PcmRingBuffer, StaleGeneration
from .timeline import AudioTimeline

log = logging.getLogger("state")

# 播放状态常量（任务书第 10 节）
STOPPED = "STOPPED"
PAUSED = "PAUSED"
PLAYING = "PLAYING"
BUFFERING = "BUFFERING"
ERROR = "ERROR"

# 渲染器上报的 UPnP 传输状态
_RENDERER_PLAYING = "PLAYING"
_RENDERER_PAUSED = "PAUSED_PLAYBACK"
_RENDERER_STOPPED = "STOPPED"
_RENDERER_TRANSITIONING = "TRANSITIONING"

# 收敛目标
_MODE_STOP = "stop"
_MODE_PLAY = "play"
_MODE_PAUSE = "pause"

# 连续 SOAP 失败多少次判定渲染器离线
_MAX_ACTION_ERRORS = 5


@dataclass
class PlaybackState:
    """对外暴露的统一播放状态。"""

    state: str = STOPPED
    position_ms: Optional[float] = None
    duration_ms: Optional[float] = None
    title: str = ""
    artist: str = ""
    album: str = ""
    album_art: Optional[bytes] = None
    album_art_mime: str = "image/jpeg"
    volume: int = 0
    muted: bool = False
    renderer_state: str = _RENDERER_STOPPED
    audio_format: str = ""
    airplay_session: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, artwork_url: Optional[str] = None) -> dict:
        return {
            "state": self.state,
            "position_ms": int(self.position_ms) if self.position_ms is not None else None,
            "duration_ms": int(self.duration_ms) if self.duration_ms is not None else None,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "artwork_url": artwork_url if self.album_art else None,
            "volume": self.volume,
            "muted": self.muted,
            "audio_format": self.audio_format,
        }


@dataclass
class _Intent:
    """收敛线程要达成的目标。"""

    mode: str = _MODE_STOP
    token: str = ""          # 要播放的流 token
    volume: Optional[int] = None
    revision: int = 0


class BridgeController:
    """把 AirPlay 事件、PCM 缓冲、DLNA 渲染器粘合在一起。"""

    #: 会话建立后的漂移宽限期（秒）：渲染器此时仍在缓冲，位置值不可比
    DRIFT_GRACE_SECONDS = 8.0
    #: 两次漂移重建之间的最小间隔（秒），避免抖动造成反复切断播放
    DRIFT_REBUILD_COOLDOWN_SECONDS = 10.0
    #: 单个播放会话内允许的漂移重建次数上限，超过则停止自动重建
    DRIFT_REBUILD_LIMIT = 5

    def __init__(self, config, registry, ring: PcmRingBuffer, timeline: AudioTimeline,
                 streams: stream.StreamManager, log=None) -> None:
        self.config = config
        self.registry = registry
        self.ring = ring
        self.timeline = timeline
        self.streams = streams
        self.log = log or (lambda msg, *a: None)

        self._lock = threading.RLock()
        self.state = PlaybackState()

        self._intent = _Intent()
        self._intent_event = threading.Event()
        self._intent_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._poller: Optional[threading.Thread] = None

        self._gen_token = ""
        #: 渲染器**实际**已经被我们设置为播放的流 token（判断是否需要下发 SOAP 的依据）
        self._renderer_token = ""
        self._paused_with_stop = False
        self._action_errors = 0
        self._pending_reanchor = False
        #: 当前 DLNA 会话的建立时刻（用于漂移宽限期）
        self._session_started_at = 0.0
        #: 漂移重建的冷却与次数控制（防止反复重建把播放切成碎片）
        self._last_drift_rebuild = 0.0
        self._drift_rebuilds = 0
        self._drift_limit_logged = False
        #: 上一轮收敛失败需要重试（由 _safe_call 置位）
        self._converge_dirty = False
        self._last_volume_tx = 0.0
        self._last_volume_rx = 0.0
        self._subscriptions: dict[str, str] = {}
        self._last_renew_monotonic = 0.0
        self._artwork_lock = threading.Lock()
        self._http_port = int(config.get("http_port"))
        self._base_url = ""
        self._on_airplay_name_change: Optional[Callable[[str], None]] = None
        self._pre_flush_hook: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self._refresh_base_url()
        self._worker = threading.Thread(target=self._worker_loop, name="dlna-worker", daemon=True)
        self._worker.start()
        self._poller = threading.Thread(target=self._poll_loop, name="dlna-poller", daemon=True)
        self._poller.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._intent_event.set()
        try:
            self.streams.close_all()
        except Exception:  # noqa: BLE001
            pass
        for thread in (self._worker, self._poller):
            if thread is not None and thread.is_alive():
                thread.join(timeout=5.0)

    def set_airplay_name_callback(self, callback: Callable[[str], None]) -> None:
        self._on_airplay_name_change = callback

    def _refresh_base_url(self) -> None:
        address = netif.primary_lan_address()
        self._base_url = f"http://{address}:{self._http_port}"

    # ------------------------------------------------------------------ 音频入口
    def on_audio_bytes(self, data: bytes) -> None:
        """音频读取线程回调：把 FIFO 数据推进环形缓冲。"""
        self.ring.append(data)

    # --------------------------------------------------------------- 元数据入口
    def on_metadata_item(self, item: MetadataItem) -> None:
        """元数据读取线程回调。**不得阻塞**，所有网络动作交给收敛线程。"""
        code = item.code
        try:
            if item.type == "ssnc":
                self._handle_ssnc(code, item)
            elif item.type == "core":
                self._handle_core(code, item)
        except Exception:  # noqa: BLE001 - 单个事件异常不能打断读循环
            log.exception("处理元数据事件失败: %s/%s", item.type, code)

    def _handle_ssnc(self, code: str, item: MetadataItem) -> None:
        if code == "conn":
            self._set_client(item.text, connected=True)
        elif code == "clip":
            with self._lock:
                self.state.airplay_session["client_ip"] = item.text
            log.info("AirPlay client connected (device=%r, ip=%s)",
                     self.state.airplay_session.get("name", ""), item.text)
        elif code == "disc":
            log.info("AirPlay 客户端断开: %s", item.text)
            self._handle_session_end("客户端断开")
        elif code == "snam":
            with self._lock:
                self.state.airplay_session["name"] = item.text
            log.info("AirPlay client device: %s", item.text)
        elif code == "snua":
            with self._lock:
                self.state.airplay_session["user_agent"] = item.text
        elif code == "svna":
            log.info("AirPlay 服务名已注册: %s", item.text)
        elif code == "styp":
            log.info("AirPlay 流类型: %s", item.text)
        elif code in ("pbeg", "pres"):
            self._handle_play(code == "pres")
        elif code == "paus":
            self._handle_pause()
        elif code in ("pend", "aend"):
            self._handle_session_end("播放结束")
        elif code == "pfls":
            self._handle_flush(item.text)
        elif code == "pdis":
            log.info("检测到时间戳不连续（按 seek 处理）: %s", item.text)
            self._handle_flush(item.text)
        elif code == "prgr":
            self.timeline.on_prgr(item.text)
            self._after_progress()
        elif code == "phbt":
            self.timeline.on_phbt(item.text, first=False)
        elif code == "phb0":
            self.timeline.on_phbt(item.text, first=True)
        elif code == "pffr":
            self.timeline.on_pffr(item.text)
        elif code == "pvol":
            self._handle_volume(item)
        elif code == "PICT":
            with self._artwork_lock:
                self.state.album_art = bytes(item.data)
                self.state.album_art_mime = _sniff_image_mime(item.data)
            log.info("收到封面图 %d 字节", len(item.data))

    def _handle_core(self, code: str, item: MetadataItem) -> None:
        text = item.text
        with self._lock:
            if code == "minm":
                self.state.title = text
            elif code == "asar":
                self.state.artist = text
            elif code == "asal":
                self.state.album = text
            elif code == "astm":
                duration = item.uint32()
                if duration:
                    self.timeline.set_metadata_duration_ms(float(duration))
            elif code == "styp":
                pass
        if code in ("minm", "asar", "asal"):
            log.info("元数据更新: title=%r artist=%r album=%r",
                     self.state.title, self.state.artist, self.state.album)

    def _set_client(self, payload: str, connected: bool) -> None:
        with self._lock:
            if connected:
                self.state.airplay_session["client_ip"] = payload
                self.state.airplay_session["connected"] = True
            else:
                self.state.airplay_session["connected"] = False
        if connected:
            log.info("AirPlay session established (client ip=%s)", payload)

    # ------------------------------------------------------------- 状态迁移
    def _handle_play(self, is_resume: bool) -> None:
        self._refresh_base_url()
        if is_resume:
            self.timeline.on_resume()
            with self._lock:
                need_rebuild = self._paused_with_stop
                token = self._gen_token
            if need_rebuild:
                log.info("恢复播放：渲染器此前无法暂停，重建 DLNA 会话")
                # 与 seek 一致：必须先换代 —— 否则新会话会从本代起点读到几十秒前的
                # 旧数据，且漂移基准（代偏移）没有更新，会立刻再次触发重建。
                self._begin_new_generation(reason="resume-rebuild", offset_ms=None)
                self._start_dlna_session(reason="resume-rebuild")
            elif token:
                log.info("恢复播放：向渲染器发送 Play")
                self._set_intent(_MODE_PLAY, token)
            else:
                self._start_dlna_session(reason="resume")
            return

        # pbeg：新的播放会话
        log.info("AirPlay session started")
        self._drift_rebuilds = 0
        self._drift_limit_logged = False
        self._last_drift_rebuild = 0.0
        self._begin_new_generation(reason="pbeg", offset_ms=None)
        self._start_dlna_session(reason="pbeg")

    def _handle_pause(self) -> None:
        self.timeline.on_pause()
        with self._lock:
            self.state.state = PAUSED
        self._set_intent(_MODE_PAUSE, self._gen_token)

    def _handle_flush(self, payload: str) -> None:
        """``pfls``：seek。载荷是要 flush 到的帧号。"""
        log.info("SetAVTransportURI 需要重锚：检测到 seek/flush (frame=%s)", payload or "?")
        self._begin_new_generation(reason="flush", offset_ms=None)
        with self._lock:
            self._pending_reanchor = True
        # AirPlay 侧的新位置通过随后的 prgr 得知，preroll 期间即可修正
        self._start_dlna_session(reason="seek")

    def _handle_session_end(self, reason: str) -> None:
        log.info("AirPlay session ended (%s)", reason)
        self.timeline.on_stop()
        self.ring.flush()
        with self._lock:
            self.state.state = STOPPED
            self.state.title = ""
            self.state.artist = ""
            self.state.album = ""
            self.state.album_art = None
            self.state.duration_ms = None
            self.state.position_ms = None
            self.state.audio_format = ""
            self.state.airplay_session = {}
            self.state.renderer_state = _RENDERER_STOPPED
            self._gen_token = ""
            self._renderer_token = ""
            self._paused_with_stop = False
            self._pending_reanchor = False
        self._drift_rebuilds = 0
        self._drift_limit_logged = False
        self._last_drift_rebuild = 0.0
        self._session_started_at = 0.0
        self.streams.close_all()
        self._set_intent(_MODE_STOP, "")

    def _handle_volume(self, item: MetadataItem) -> None:
        values = item.csv_numbers()
        if not values:
            log.debug("无法解析 pvol 载荷: %r", item.text)
            return
        airplay_db = values[0]
        percent, muted = stream.airplay_db_to_percent(airplay_db)
        with self._lock:
            # 抑制渲染器回声：刚由我们下发过的音量值不再回灌
            if time.monotonic() - self._last_volume_rx < 1.0:
                return
            self.state.volume = percent
            self.state.muted = muted
            self._last_volume_tx = time.monotonic()
        log.info("SetVolume: AirPlay %.2f dB -> %d%%%s", airplay_db, percent,
                 " (mute)" if muted else "")
        self._set_intent(self._intent.mode, self._intent.token, volume=percent)

    def _after_progress(self) -> None:
        """收到 prgr 后：修正代偏移、刷新时长。"""
        duration = self.timeline.duration_ms()
        position = self.timeline.position_ms()
        with self._lock:
            pending = self._pending_reanchor
            if pending:
                self._pending_reanchor = False
        if pending:
            self.timeline.refine_generation_offset(position)
            log.info("seek 后重锚：新的曲目位置约为 %s ms",
                     int(position) if position is not None else "未知")
        if duration:
            self.streams.update_duration(duration)

    # ------------------------------------------------------------- 代管理
    def set_pre_flush_hook(self, hook: Callable[[], None]) -> None:
        """注册「换代前」钩子。

        seek 时管道里可能仍残留 seek 之前的音频（pipe 后端不知道 flush 事件），
        必须先丢弃，否则旧音频会被写进新一代的开头。
        """
        self._pre_flush_hook = hook

    def _begin_new_generation(self, reason: str, offset_ms: Optional[float]) -> None:
        if self._pre_flush_hook is not None:
            try:
                self._pre_flush_hook()
            except Exception:  # noqa: BLE001
                log.debug("换代前钩子执行失败", exc_info=True)
        generation = self.ring.flush()
        kind, _content_type = "wav", "audio/wav"
        with self._lock:
            self.state.duration_ms = None
            self.state.position_ms = None
            self.state.state = BUFFERING
        self.timeline.begin_generation(generation, offset_ms)
        log.info("音频缓冲换代: gen=%d 原因=%s", generation, reason)

    # ------------------------------------------------------------- 意图下发
    def _set_intent(self, mode: str, token: str, volume: Optional[int] = None) -> None:
        with self._intent_lock:
            self._intent.revision += 1
            self._intent.mode = mode
            if token:
                self._intent.token = token
            if volume is not None:
                self._intent.volume = volume
        self._intent_event.set()

    def _start_dlna_session(self, reason: str) -> None:
        """创建新一代流并请求收敛线程去播放它。"""
        record = self.registry.selected()
        if record is None:
            log.warning("尚未选择 DLNA Renderer，无法开始播放（%s）", reason)
            with self._lock:
                self.state.state = ERROR
            return
        # resolve_stream_kind 返回 (content_type, kind)，注意顺序
        _content_type, kind = self.registry.resolve_stream_kind(
            record, self.config.get("output_format")
        )
        duration = self.timeline.duration_ms()
        session = self.streams.new_generation(kind, duration)
        with self._lock:
            self._gen_token = session.token
            self._paused_with_stop = False
        self._set_intent(_MODE_PLAY, session.token)

    # ------------------------------------------------------- 收敛线程
    def _worker_loop(self) -> None:
        """收敛线程。

        只在「意图版本号变化」或「上一轮失败需要重试」时才重新下发 SOAP。
        早期实现每次超时（1s）都无条件重跑一遍，会把 Stop / SetVolume 每秒重复
        发给渲染器，属于严重的 SOAP 风暴（任务书第 12 节明确禁止）。
        """
        last_revision = -1
        while not self._stop_event.is_set():
            self._intent_event.wait(timeout=1.0)
            self._intent_event.clear()
            if self._stop_event.is_set():
                return
            with self._intent_lock:
                intent = _Intent(
                    mode=self._intent.mode,
                    token=self._intent.token,
                    volume=self._intent.volume,
                    revision=self._intent.revision,
                )
            retrying = self._converge_dirty and intent.revision == last_revision
            if intent.revision == last_revision and not self._converge_dirty:
                continue
            if retrying:
                # 失败重试：给渲染器留出恢复时间，避免每秒重击
                if self._stop_event.wait(3.0):
                    return
            last_revision = intent.revision
            self._converge_dirty = False
            try:
                self._converge(intent)
            except Exception:  # noqa: BLE001 - 收敛线程必须长期存活
                log.exception("DLNA 收敛过程异常")
                self._converge_dirty = True
                time.sleep(1.0)

    def _converge(self, intent: _Intent) -> None:
        record = self.registry.selected()
        client = record.client if record is not None else None
        if record is None or client is None:
            if intent.mode != _MODE_STOP:
                log.warning("Renderer 未选中或不可用，无法执行 %s", intent.mode)
            return

        # 音量是独立维度，每次都尝试同步
        if intent.volume is not None:
            self._apply_volume(record, client, intent.volume)

        if intent.mode == _MODE_STOP:
            self._safe_call(record, client.stop, "Stop")
            with self._lock:
                self.state.renderer_state = _RENDERER_STOPPED
                self.state.state = STOPPED
                self._renderer_token = ""
            return

        if intent.mode == _MODE_PAUSE:
            self._do_pause(record, client)
            return

        if intent.mode == _MODE_PLAY:
            self._do_play(record, client, intent.token)

    def _do_play(self, record, client, token: str) -> None:
        session = self.streams.get(token)
        if session is None:
            log.warning("流会话不存在（可能已过期）: token=%s", token)
            return

        current_renderer_state = self.state.renderer_state
        # 注意：必须与「渲染器实际在播的 token」比较，而不是与最新创建的 token 比较。
        # 否则 seek/换代后新 token 与自己相等，会被误判为「已经在播」而什么都不做。
        already_this_token = session.token == self._renderer_token

        # 情况 1：仍在播放同一条流 —— 无需动作
        if already_this_token and current_renderer_state == _RENDERER_PLAYING:
            with self._lock:
                self.state.state = PLAYING
            return

        # 情况 2：同一条流被暂停 —— 直接 Play 续播
        if already_this_token and current_renderer_state == _RENDERER_PAUSED:
            if self._safe_call(record, client.play, "Play"):
                with self._lock:
                    self.state.state = PLAYING
                    self.state.renderer_state = _RENDERER_PLAYING
                    self._paused_with_stop = False
                return
            log.warning("渲染器不接受续播，改为重建会话")

        # 情况 3：全新开始 / seek 后重建
        needed = int(self.config.get("preroll_seconds") * self.ring.byte_rate)
        generation = session.generation
        if not self.ring.wait_for_data(0, generation, needed, timeout=8.0):
            try:
                available = self.ring.write_offset
            except Exception:  # noqa: BLE001
                available = 0
            log.warning(
                "预滚动等待不足（需要 %.1fs，实际 %.1fs），仍尝试开始播放",
                self.config.get("preroll_seconds"),
                available / float(self.ring.byte_rate or 1),
            )

        # 时长可能在预滚动期间才通过 prgr 得到，这里再取一次
        duration = self.timeline.duration_ms()
        self.streams.update_duration(duration)
        session = self.streams.get(token) or session

        uri = f"{self._base_url}{session.path()}"
        metadata = stream.build_didl_lite(
            uri=uri,
            protocol_info=session.protocol_info,
            title=self.state.title,
            artist=self.state.artist,
            album=self.state.album,
            artwork_url=f"{self._base_url}/api/artwork" if self.state.album_art else "",
            duration_ms=session.duration_ms,
            sample_rate=session.sample_rate,
            channels=session.channels,
            bits=session.bits,
        )

        log.info("SetAVTransportURI %s (type=%s)", uri, session.content_type)
        if not self._safe_call(record, client.set_av_transport_uri, "SetAVTransportURI",
                               uri=uri, metadata=metadata):
            return
        # 部分渲染器在 SetAVTransportURI 之后需要短暂准备
        time.sleep(0.3)
        if not self._safe_call(record, client.play, "Play"):
            return
        with self._lock:
            self.state.state = PLAYING
            self.state.renderer_state = _RENDERER_PLAYING
            self._paused_with_stop = False
            self._renderer_token = session.token
        self.timeline.update_renderer_position(0.0)
        # 记录会话建立时刻：渲染器此时还在缓冲、位置会滞后，宽限期内不做漂移判定
        self._session_started_at = time.monotonic()
        self._ensure_subscriptions(record)

    def _do_pause(self, record, client) -> None:
        """优先使用 UPnP Pause；渲染器不支持时退化为 Stop（记住需重建）。"""
        if self.state.renderer_state == _RENDERER_STOPPED:
            with self._lock:
                self.state.state = PAUSED
            return
        if self._safe_call(record, client.pause, "Pause"):
            with self._lock:
                self.state.state = PAUSED
                self.state.renderer_state = _RENDERER_PAUSED
            return
        log.warning(
            "渲染器不支持暂停实时流（Pause 失败），退化为 Stop；恢复时将重建会话"
        )
        if self._safe_call(record, client.stop, "Stop"):
            with self._lock:
                self.state.state = PAUSED
                self.state.renderer_state = _RENDERER_STOPPED
                self._paused_with_stop = True
                self._renderer_token = ""

    def _apply_volume(self, record, client, percent: int) -> None:
        try:
            current = client.get_volume()
        except Exception:  # noqa: BLE001
            current = None
        with self._lock:
            if current is not None:
                self.state.volume = current
            if abs((current if current is not None else -1) - percent) <= 1:
                self._last_volume_tx = time.monotonic()
                return
        if self._safe_call(record, client.set_volume, "SetVolume", volume=int(percent)):
            with self._lock:
                self.state.volume = int(percent)
                self._last_volume_tx = time.monotonic()

    # ------------------------------------------------------- SOAP 调用包装
    def _safe_call(self, record, func, action: str, **kwargs) -> bool:
        """带超时/异常处理与错误计数的 SOAP 调用。失败会置脏以触发重试。"""
        try:
            if action == "SetAVTransportURI":
                func(kwargs["uri"], kwargs["metadata"])
            elif action == "SetVolume":
                func(kwargs["volume"])
            else:
                func()
        except upnp.UpnpError as exc:
            self._note_action_failure(record, action, str(exc))
            self._converge_dirty = True
            return False
        except Exception as exc:  # noqa: BLE001
            self._note_action_failure(record, action, f"{type(exc).__name__}: {exc}")
            self._converge_dirty = True
            return False
        self._action_errors = 0
        return True

    def _note_action_failure(self, record, action: str, detail: str) -> None:
        self._action_errors += 1
        log.error("DLNA SOAP 失败 (action=%s, udn=%s): %s", action, record.udn, detail)
        if self._action_errors >= _MAX_ACTION_ERRORS and record is not None:
            self.registry.mark_offline(record.udn, f"{action} 连续失败")
            with self._lock:
                self.state.state = ERROR
                self.state.renderer_state = "OFFLINE"

    # ------------------------------------------------------------ 位置轮询
    def _poll_loop(self) -> None:
        self._stop_event.wait(2.0)
        while not self._stop_event.is_set():
            interval = float(self.config.get("metadata_poll_seconds"))
            try:
                self._poll_once()
                self._renew_subscriptions_if_due()
            except Exception:  # noqa: BLE001
                log.exception("位置轮询异常")
            # 加 ±20% 抖动，避免与设备自身的定时任务共振
            import random

            self._stop_event.wait(interval * random.uniform(0.8, 1.2))

    def _renew_subscriptions_if_due(self) -> None:
        """GENA 订阅按 1800s 周期续订（到期前 600s 续订一次）。"""
        now = time.monotonic()
        if now - self._last_renew_monotonic < 1200.0:
            return
        self._last_renew_monotonic = now
        record = self.registry.selected()
        if record is None or record.client is None:
            return
        for key, sid in list(self._subscriptions.items()):
            if not key.startswith(record.udn + "|"):
                continue
            try:
                if record.client.renew_subscription(sid, timeout_s=1800):
                    log.debug("GENA 续订成功: %s", sid)
                else:
                    log.info("GENA 续订失败，移除订阅并回退到轮询: %s", sid)
                    self._subscriptions.pop(key, None)
            except Exception as exc:  # noqa: BLE001
                log.debug("GENA 续订异常: %s", exc)
                self._subscriptions.pop(key, None)

    def _poll_once(self) -> None:
        record = self.registry.selected()
        if record is None or record.client is None:
            return
        client = record.client
        try:
            info = client.get_transport_info()
        except Exception as exc:  # noqa: BLE001
            self._note_action_failure(record, "GetTransportInfo", str(exc))
            return

        renderer_state = (info.get("state") or "").upper() or _RENDERER_STOPPED
        with self._lock:
            self.state.renderer_state = renderer_state
            if renderer_state == _RENDERER_PLAYING and self.state.state in (BUFFERING, PAUSED):
                self.state.state = PLAYING
            elif renderer_state == _RENDERER_PAUSED and self.state.state == PLAYING:
                self.state.state = PAUSED
        self._action_errors = 0

        if renderer_state in (_RENDERER_PLAYING, _RENDERER_PAUSED):
            try:
                position = client.get_position_info()
            except Exception as exc:  # noqa: BLE001
                log.debug("GetPositionInfo 失败: %s", exc)
                position = None
            if position:
                rel = position.get("rel_time_ms")
                duration = position.get("duration_ms")
                self.timeline.update_renderer_position(
                    float(rel) if rel is not None else None
                )
                if duration is None:
                    duration = self.timeline.duration_ms()
                with self._lock:
                    self.state.duration_ms = duration
                self._check_drift(record)

        self._sync_positions()

    def _check_drift(self, record) -> None:
        drift = self.timeline.drift_ms()
        if drift is None:
            return
        # 会话刚建立时渲染器仍在缓冲，rel_time 尚未反映真实播放位置，
        # 此时比较只会得到假漂移（并触发无意义的重建）。
        if time.monotonic() - self._session_started_at < self.DRIFT_GRACE_SECONDS:
            return
        threshold = float(self.config.get("drift_threshold_ms"))
        if abs(drift) <= threshold:
            return
        if self._drift_rebuilds >= self.DRIFT_REBUILD_LIMIT:
            if not self._drift_limit_logged:
                self._drift_limit_logged = True
                log.warning(
                    "漂移反复出现（本会话已重建 %d 次，最近偏差 %.0f ms），"
                    "停止自动重建以免把播放切成碎片：%s（可重新播放或切换设备恢复对齐）",
                    self._drift_rebuilds, drift, record.name,
                )
            return
        now = time.monotonic()
        if now - self._last_drift_rebuild < self.DRIFT_REBUILD_COOLDOWN_SECONDS:
            log.debug("漂移 %.0f ms 仍在重建冷却期内，本次跳过", drift)
            return
        self._drift_rebuilds += 1
        self._last_drift_rebuild = now
        log.warning(
            "检测到时间线漂移 %.0f ms（阈值 %.0f ms），重建 DLNA 会话以对齐: %s（第 %d 次）",
            drift, threshold, record.name, self._drift_rebuilds,
        )
        # **必须换代**：
        # ① 不换代时渲染器从本代 offset=0 重新拉流，读到的是这一代里最早的数据
        #    （可能已是几十秒前的音频）—— 听感就是同一小段被反复重播；
        # ② 换代时 begin_generation() 会把「当前曲目位置」记为新基准偏移，
        #    漂移检测才有正确的参照。否则漂移恒等于曲目绝对位置，每 2~3 秒
        #    重建一次，形成死循环。
        self._begin_new_generation(reason="drift", offset_ms=None)
        self._start_dlna_session(reason="drift")

    def _sync_positions(self) -> None:
        position = self.timeline.position_ms()
        duration = self.timeline.duration_ms()
        with self._lock:
            self.state.position_ms = position
            if duration:
                self.state.duration_ms = duration
            if not self.state.audio_format:
                self.state.audio_format = self._describe_format()

    def _describe_format(self) -> str:
        """尽力描述当前音频格式（来自 shairport-sync 的输出配置）。"""
        rate = self.ring.sample_rate
        channels = self.ring.channels
        bits = self.ring.sample_width * 8
        stream_type = self.state.airplay_session.get("stream_type", "")
        codec = stream_type or "PCM"
        return f"{codec} {rate}Hz {bits}bit {channels}ch"

    # ------------------------------------------------------------ GENA 订阅
    def _ensure_subscriptions(self, record) -> None:
        """订阅 AVTransport 与 RenderingControl 事件（失败则完全依赖轮询）。"""
        if record.client is None:
            return
        callback = f"{self._base_url}/upnp/notify/{record.udn.replace(':', '_')}"
        for service_urn in (upnp.AVTRANSPORT, upnp.RENDERINGCONTROL):
            key = f"{record.udn}|{service_urn}"
            if key in self._subscriptions:
                continue
            try:
                sid = record.client.subscribe(callback, timeout_s=1800)
            except Exception as exc:  # noqa: BLE001
                log.info("GENA 订阅失败（将使用轮询）: %s (%s)", service_urn.rsplit(':', 2)[-2], exc)
                continue
            if sid:
                self._subscriptions[key] = sid
                log.info("GENA 已订阅: %s sid=%s", service_urn.rsplit(':', 2)[-2], sid)

    def on_notify(self, udn_token: str, body: bytes) -> None:
        """处理渲染器推送的 GENA NOTIFY（由 HTTP 层调用）。"""
        try:
            changes = _parse_last_change(body)
        except Exception:  # noqa: BLE001
            log.debug("无法解析 GENA NOTIFY")
            return
        if not changes:
            return
        with self._lock:
            if "TransportState" in changes:
                self.state.renderer_state = changes["TransportState"].upper()
            if "Volume" in changes:
                try:
                    volume = int(float(changes["Volume"]))
                except ValueError:
                    volume = None
                if volume is not None and time.monotonic() - self._last_volume_tx > 1.0:
                    self.state.volume = max(0, min(100, volume))
                    self._last_volume_rx = time.monotonic()
            if "Mute" in changes:
                self.state.muted = changes["Mute"] in ("1", "true", "True", "yes")
            rel = changes.get("RelativeTimePosition")
            if rel:
                ms = _hms_to_ms(rel)
                if ms is not None:
                    self.timeline.update_renderer_position(float(ms))
            duration = changes.get("CurrentTrackDuration")
            if duration:
                ms = _hms_to_ms(duration)
                if ms:
                    self.state.duration_ms = ms

    # ------------------------------------------------------------- 对外查询
    def set_renderer(self, udn: str) -> None:
        record = self.registry.set_selected(udn)
        try:
            self.config.update({
                "selected_renderer_udn": record.udn,
                "selected_renderer_name": record.name,
                "selected_renderer_ip": record.ip,
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("保存 Renderer 选择失败: %s", exc)
        # 切换设备时结束旧会话
        self._set_intent(_MODE_STOP, "")
        with self._lock:
            self.state.renderer_state = _RENDERER_STOPPED
            self._renderer_token = ""

    def artwork(self) -> tuple[Optional[bytes], str]:
        with self._artwork_lock:
            return self.state.album_art, self.state.album_art_mime

    def status(self) -> dict:
        with self._lock:
            self._sync_positions()
            record = self.registry.selected()
            playback = self.state.to_dict(
                artwork_url="/api/artwork" if self.state.album_art else None
            )
            renderer_info: Optional[dict] = None
            if record is not None:
                renderer_info = {
                    "udn": record.udn,
                    "name": record.name,
                    "ip": record.ip,
                    "model": record.model,
                    "manufacturer": record.manufacturer,
                    "online": record.online,
                    "state": self.state.renderer_state,
                    "volume": self.state.volume,
                    "muted": self.state.muted,
                    "supported_mime": record.supported_mime,
                    "capability_error": record.capability_error,
                }
            return {
                "playback": playback,
                "renderer": renderer_info,
                "timeline": self.timeline.snapshot(),
                "buffer": self.ring.stats(),
                "intent": {"mode": self._intent.mode, "token": self._intent.token},
            }


# --------------------------------------------------------------------- 小工具
def _sniff_image_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"


def _hms_to_ms(value: str) -> Optional[int]:
    """``HH:MM:SS`` 或 ``HH:MM:SS.mmm`` -> 毫秒。"""
    try:
        text = value.strip()
        if not text or text.upper().startswith("NOT_IMPLEMENTED"):
            return None
        parts = text.split(":")
        if len(parts) != 3:
            return None
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
        return int((hours * 3600 + minutes * 60 + seconds) * 1000)
    except (ValueError, IndexError):
        return None


def _parse_last_change(body: bytes) -> dict[str, str]:
    """从 GENA ``LastChange`` 事件 XML 中提取变量名 -> 值。

    兼容两种常见形态：``propertyset/property/LastChange``（值为转义后的 XML）
    以及直接内嵌的 ``<Volume channel="Master" val="42"/>``。
    """
    import re
    from xml.etree import ElementTree

    text = body.decode("utf-8", "replace")
    result: dict[str, str] = {}

    # 形态 1：LastChange 内的嵌套 XML（属性 val= 形式）
    for match in re.finditer(r"<([A-Za-z][\w:]*)\b([^>]*?)/?>", text):
        tag = match.group(1)
        attrs = match.group(2)
        val_match = re.search(r'\bval\s*=\s*"([^"]*)"', attrs)
        if val_match:
            result[tag] = val_match.group(1)

    # 形态 2：<Tag>value</Tag>
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return result
    for element in root.iter():
        tag = element.tag.split("}")[-1]
        if element.text and element.text.strip() and tag not in result:
            result.setdefault(tag, element.text.strip())
    return result
