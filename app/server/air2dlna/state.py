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
from collections import deque
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
    #: 是否允许由该意图触发 ``SetAVTransportURI``。
    #: 复用同一个 generation 时必须为 False —— DLNA 语义下重设 URI 会让渲染器
    #: 从该资源的**开头**重新播放（真机现象：「暂停后恢复变成从头播放」）。
    set_uri: bool = True


class BridgeController:
    """把 AirPlay 事件、PCM 缓冲、DLNA 渲染器粘合在一起。"""

    #: 「渲染器没在拉流」多久后触发兜底重建（秒）
    STREAM_STALL_SECONDS = 8.0
    #: AirPlay `pend`（播放流结束）后的过渡窗口（秒）。
    #: pend 不等价于会话结束：seek / 换曲 / 暂停都可能触发它，立刻 Stop DLNA 会让
    #: iPhone 拖动进度条或暂停后长时间无声。窗口内收到新的流事件就继续播放，
    #: 超时才按真正的播放结束处理。
    #: 取 12 秒是为了覆盖「暂停一会儿再继续」——真结束只是晚几秒停，无副作用。
    PEND_TRANSITION_TIMEOUT_SECONDS = 12.0
    #: Renderer 重建（换 generation / 换 URI）后的最小间隔（秒），防止风暴
    REBUILD_COOLDOWN_SECONDS = 10.0
    #: 诊断日志输出间隔（秒）
    DIAGNOSTIC_INTERVAL_SECONDS = 15.0
    #: 判定「渲染器时钟速率异常」的相对偏差容差（ΔRelTime / ΔAirPlay）。
    #: 采样粒度是 1 秒，正常读数本就有 ±0.3 量级抖动，容差太小会被噪声淹没。
    RELTIME_RATE_TOLERANCE = 0.20
    #: 速率告警的最小间隔（秒）：真漂移会持续存在，不必每条采样都记
    RELTIME_RATE_WARN_INTERVAL = 60.0
    #: ``pbeg`` 时判断「播放位置是否连续」的容差，比较的是**曲目位置本身的变化**
    #: （暂停期间它几乎不动，seek 会跳变）。
    #: 注意：不能用「AirPlay 位置 −（代偏移 + 渲染器 RelTime）」的绝对值当判据 ——
    #: 那个量包含了渲染器缓冲延迟与代偏移误差，真机上可达几十秒，会把正常暂停
    #: 误判成跳变（1.0.7 的 bug，导致暂停恢复后从头播）。
    RESUME_POSITION_TOLERANCE_MS = 4000.0
    #: 刚刚处理过真实 seek（``pfls``/``pdis``）的窗口（秒）。seek 已经换代了，
    #: 紧随其后的 ``pbeg`` 不许再来一次（否则音箱重复缓冲）。
    SEEK_REUSE_WINDOW_SECONDS = 10.0

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
        #: 当前 DLNA 会话的建立时刻
        self._session_started_at = 0.0
        #: 重建冷却（stalled 兜底 / 过渡超时等都用它，避免重建风暴）
        self._last_rebuild_at = 0.0
        #: 上次因「渲染器没在拉流」而重建的时刻
        self._last_stall_rebuild = 0.0
        #: pend 过渡态
        self._awaiting_new_stream = False
        self._transition_deadline = 0.0
        self._transition_reason = ""
        #: 诊断：DLNA 侧位置只作观测
        self._rel_sample: Optional[tuple[float, float]] = None
        self._rel_offset_ms: Optional[float] = None
        self._rel_rate: Optional[float] = None
        self._rel_observed_at = 0.0
        self._rel_rate_warned_at = 0.0
        #: 进入过渡态 / 暂停那一刻的曲目位置，用于判断 pbeg 时的位置连续性
        self._position_at_stream_boundary: Optional[float] = None
        #: 最近一次因真实 seek 换代的时刻
        self._handled_seek_at = 0.0
        self._last_range_info = ""
        self._last_diag_log = 0.0
        #: 每次 SetAVTransportURI 的序号与原因（验收要求：正常播放应该只有 1 次）
        self._uri_count = 0
        self._uri_log: list[str] = []
        self._last_rebuild_reason = ""
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
        if self._awaiting_new_stream:
            # 仍有 PCM 进来 → 播放流并未真正结束（只是事件次序），立刻退出过渡态
            self._cancel_transition("仍在推送 PCM")

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
        elif code == "pend":
            # pend = AirPlay **播放流**结束，不能当成整个会话结束：
            # 拖动进度条、切歌、暂停都可能只发 pend 并紧跟新的流事件。
            log.info("AirPlay 事件: pend（播放流结束）")
            self._handle_play_stream_end("播放流结束")
        elif code == "aend":
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
                # 暂停恢复 = 同一个播放位置的继续：只发 Play，**绝不重设 URI**。
                # 真机上渲染器收到 Pause 后会自行转入 STOPPED（诊断日志可见
                # state=PAUSED renderer=STOPPED）。此时若下发 SetAVTransportURI，
                # DLNA 语义会让它从该资源**开头**重新播放 —— 这正是「暂停后恢复
                # 变成从头播放」的直接原因（1.0.8 只改了 pbeg 分支，而真机走的是
                # 这里的 pres/resume 分支）。
                log.info("恢复播放：向渲染器发送 Play（沿用当前 DLNA 会话 gen=%d，不重设 URI）",
                         self.ring.generation)
                self._set_intent(_MODE_PLAY, token, set_uri=False)
            else:
                self._start_dlna_session(reason="resume")
            return

        # pbeg：新的播放会话
        log.info("AirPlay session started")
        self._cancel_transition("pbeg")

        if self._can_reuse_generation():
            # AirPlay 2 在「暂停后恢复」和「seek 之后」都会发 ``pend`` + ``pbeg``。
            # 此时音频数据是连续的，重建会话只会让音箱重新缓冲并淡入 ——
            # 真机表现就是卡顿甚至直接停止。这里沿用当前 generation，
            # 只确保渲染器处于播放状态（Play 是幂等的）。
            log.info("pbeg：沿用当前 DLNA 会话 gen=%d（不重建、不重设 URI）",
                     self.ring.generation)
            self._set_intent(_MODE_PLAY, self._gen_token, set_uri=False)
            return

        # 真正的新播放位置（首次播放 / 换曲 / seek 到别处）才更换媒体生命周期。
        # 重建计数与 SetAVTransportURI 计数归零（验收：一次正常播放只应 1 次）。
        self._last_rebuild_at = 0.0
        self._uri_count = 0
        self._begin_new_generation(reason="pbeg", offset_ms=None)
        self._start_dlna_session(reason="pbeg")

    def _can_reuse_generation(self) -> bool:
        """``pbeg`` 时判断能否沿用当前 DLNA 会话。

        判据是**曲目位置本身是否连续** —— 暂停期间它几乎不动，换曲/跳到别处会大幅跳变。
        不能用「AirPlay 位置 −（代偏移 + 渲染器 RelTime）」的绝对值当判据：那个量含
        渲染器缓冲延迟与代偏移误差，真机可达几十秒，会把正常暂停误判成跳变
        （1.0.7 的 bug：暂停恢复后每次都换代 → 从头播放）。

        另外：若刚刚因真实 seek 换代过，紧随的 ``pbeg`` 直接沿用（seek 已换代）。
        """
        with self._lock:
            token = self._gen_token
            stopped = self.state.state == STOPPED
            baseline = self._position_at_stream_boundary
        if stopped or not token:
            return False
        session = self.streams.get(token)
        if session is None or session.closed:
            return False
        now = time.monotonic()
        if now - self._handled_seek_at <= self.SEEK_REUSE_WINDOW_SECONDS:
            log.info("pbeg：%.1fs 前刚处理过真实 seek（已换代），沿用当前 DLNA 会话",
                     now - self._handled_seek_at)
            return True
        current = self.timeline.position_ms()
        if baseline is None or current is None:
            return True
        delta = abs(current - baseline)
        if delta > self.RESUME_POSITION_TOLERANCE_MS:
            log.info("pbeg：曲目位置跳变 %.0f ms（容差 %.0f ms），按新的播放位置重建",
                     delta, self.RESUME_POSITION_TOLERANCE_MS)
            return False
        log.info("pbeg：曲目位置连续（变化 %.0f ms），沿用当前 DLNA 会话", delta)
        return True

    def _handle_pause(self) -> None:
        """``paus``：暂停。优先用 UPnP Pause（不换代、不 flush，恢复即可继续）。"""
        log.info("AirPlay 暂停：向渲染器发送 Pause（沿用当前 DLNA 会话 gen=%d）",
                 self.ring.generation)
        with self._lock:
            self._position_at_stream_boundary = self.timeline.position_ms()
        self.timeline.on_pause()
        with self._lock:
            self.state.state = PAUSED
        self._set_intent(_MODE_PAUSE, self._gen_token)

    def _handle_flush(self, payload: str) -> None:
        """``pfls`` / ``pdis``：真实 seek。载荷是要 flush 到的帧号。

        这是**唯一**由 AirPlay 侧驱动的媒体生命周期变更（需求 A 类）：
        flush 旧 PCM → 新 generation → 新 token/URI → SetAVTransportURI → Play。
        """
        self._cancel_transition("seek/flush")
        self._handled_seek_at = time.monotonic()
        log.info("检测到 seek/flush (frame=%s)：换代并重锚 DLNA 会话", payload or "?")
        self._begin_new_generation(reason="flush", offset_ms=None)
        with self._lock:
            self._pending_reanchor = True
        # AirPlay 侧的新位置通过随后的 prgr 得知，preroll 期间即可修正
        self._start_dlna_session(reason="seek")

    def _handle_play_stream_end(self, reason: str) -> None:
        """``pend``：AirPlay 播放流结束 —— **不等价于**整个会话结束。

        iPhone 拖动进度条、切歌、短暂停顿都可能只发 ``pend``，随后紧跟
        ``pbeg`` 或新的 PCM。旧实现在这里直接 Stop DLNA + flush 缓冲 + 清空
        session，真机上表现为「拖完进度条后长时间无声」（实测约 79 秒空窗）。
        改为进入短过渡态：DLNA 侧保持原样，等后续事件；超时才真正结束。
        """
        with self._lock:
            if self.state.state == STOPPED:
                log.info("AirPlay 播放流结束（%s）；当前已是停止态，无需过渡", reason)
                return
            self._awaiting_new_stream = True
            self._transition_deadline = time.monotonic() + self.PEND_TRANSITION_TIMEOUT_SECONDS
            self._transition_reason = reason
            # 记下此刻的曲目位置：pbeg 到来时用它判断「是同一个位置继续」还是「跳变」
            self._position_at_stream_boundary = self.timeline.position_ms()
        log.info(
            "AirPlay 播放流结束（%s）：进入过渡态，DLNA 会话保持不变；%.1fs 内没有新的流才真正停止",
            reason, self.PEND_TRANSITION_TIMEOUT_SECONDS)

    def _cancel_transition(self, why: str) -> None:
        """退出 pend 过渡态：继续使用当前 generation/token。"""
        with self._lock:
            if not self._awaiting_new_stream:
                return
            self._awaiting_new_stream = False
            self._transition_deadline = 0.0
        log.info("过渡态结束（%s）：继续沿用当前 DLNA 会话 gen=%d", why, self.ring.generation)

    def _check_transition_timeout(self) -> None:
        """收敛线程轮询：过渡窗口内没有新流事件，才按真正的播放结束处理。"""
        with self._lock:
            if not self._awaiting_new_stream:
                return
            if time.monotonic() < self._transition_deadline:
                return
            self._awaiting_new_stream = False
        log.info("过渡态超时（%.1fs 内没有新的 AirPlay 流），按播放结束处理",
                 self.PEND_TRANSITION_TIMEOUT_SECONDS)
        self._handle_session_end("过渡超时")

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
        with self._lock:
            self._awaiting_new_stream = False
            self._transition_deadline = 0.0
        self._session_started_at = 0.0
        self._rel_sample = None
        self._rel_offset_ms = None
        self._rel_rate = None
        self._last_rebuild_at = 0.0
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
        self._last_rebuild_reason = reason
        self._last_rebuild_at = time.monotonic()
        with self._lock:
            # 换代意味着「本代第 0 字节对应哪个曲目位置」需要重设。此刻 AirPlay 的
            # prgr 往往还没到（pbeg 场景），基准会退化成 0/上一代的值；统一标记，
            # 等随后的 prgr 用真位置修正（_after_progress → refine_generation_offset）。
            self._pending_reanchor = True
        generation = self.ring.flush()
        kind, _content_type = "wav", "audio/wav"
        with self._lock:
            self.state.duration_ms = None
            self.state.position_ms = None
            self.state.state = BUFFERING
        self.timeline.begin_generation(generation, offset_ms)
        log.info("音频缓冲换代: gen=%d 原因=%s", generation, reason)

    # ------------------------------------------------------------- 意图下发
    def _set_intent(self, mode: str, token: str, volume: Optional[int] = None,
                    set_uri: bool = True) -> None:
        with self._intent_lock:
            self._intent.revision += 1
            self._intent.mode = mode
            self._intent.set_uri = set_uri
            if token:
                self._intent.token = token
            if volume is not None:
                self._intent.volume = volume
        self._intent_event.set()

    def _start_dlna_session(self, reason: str) -> None:
        """创建新一代流并请求收敛线程去播放它。"""
        self._uri_reason = reason
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
                    set_uri=self._intent.set_uri,
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
            self._do_play(record, client, intent.token, set_uri=intent.set_uri)

    def _do_play(self, record, client, token: str, set_uri: bool = True) -> None:
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

        # 情况 2b：复用同一个 generation，但渲染器不在 PLAYING/PAUSED
        #（例如它自己转成了 STOPPED）—— 只发 Play，**绝不重设 URI**：
        # DLNA 语义下 SetAVTransportURI 会让渲染器从该资源**开头**重新播放，
        # 真机现象就是「暂停后恢复变成从头播放」。
        if already_this_token and not set_uri:
            if self._safe_call(record, client.play, "Play"):
                log.info("复用当前流：仅发送 Play（未重设 URI），渲染器从原处继续")
                with self._lock:
                    self.state.state = PLAYING
                    self.state.renderer_state = _RENDERER_PLAYING
                    self._paused_with_stop = False
                return
            log.warning("渲染器拒绝 Play，回退为重建会话（SetAVTransportURI）")

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

        self._uri_count += 1
        self._uri_log.append(
            "#%d reason=%s gen=%d t=%s"
            % (self._uri_count, self._uri_reason, session.generation,
               time.strftime("%H:%M:%S"))
        )
        del self._uri_log[:-20]
        log.info(
            "SetAVTransportURI #%d reason=%s generation=%d renderer=%s uri=%s (type=%s)",
            self._uri_count, self._uri_reason, session.generation, record.name, uri,
            session.content_type)
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
                # pend 过渡窗口到期检查（不依赖渲染器是否可达）
                self._check_transition_timeout()
                self._log_diagnostics()
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
                self._observe_renderer_timeline()

        self._check_renderer_stream()

        self._sync_positions()

    def _check_renderer_stream(self) -> None:
        """兜底：渲染器声称在播放，却长时间没有拉取音频流。

        典型场景是 iPhone 拖动进度条（或暂停再恢复）后渲染器不再重连 ——
        听感就是「拖动之后声音直接停了」。这种故障用 SOAP 轮询看不出来
        （GetTransportInfo 仍回报 PLAYING），只能自己检查有没有客户端在拉流，
        随后重建会话把它拉回来。
        """
        if self.state.state != PLAYING or not self._renderer_token:
            return
        session = self.streams.get(self._renderer_token)
        if session is None or session.closed or session.clients > 0:
            return
        idle = time.monotonic() - (session.last_activity or session.created_at)
        if idle <= self.STREAM_STALL_SECONDS:
            return
        now = time.monotonic()
        if now - self._last_stall_rebuild <= self.REBUILD_COOLDOWN_SECONDS:
            return
        self._last_stall_rebuild = now
        log.warning("渲染器 %.0fs 未拉取音频流（seek/恢复后未重连），重建 DLNA 会话", idle)
        self._begin_new_generation(reason="stalled", offset_ms=None)
        self._start_dlna_session(reason="stalled")

    def _observe_renderer_timeline(self) -> None:
        """把 DLNA 侧的位置信息当作**观测值**：只诊断，绝不驱动重建。

        架构约定（1.0.5）：

        * **AirPlay / Shairport Sync 的时间线是唯一权威时间线**；
        * DLNA 的 ``RelTime``、``GetPositionInfo``、渲染器内部缓冲延迟、
          HTTP ``Range``、HTTP 重连都只是渲染器的状态 / 进度 / 健康信息；
        * 渲染器报告的绝对位置与 AirPlay 时间线存在**固定偏差**时，那是
          渲染器缓冲延迟（真机实测音箱约 3~4 秒），**不是漂移**。据此换代重建
          会让音箱重新淡入 —— 听感正是「播放十几秒后声音突然变小又变大」；
        * 因此这里只计算并记录两个量：

          - ``offset``：AirPlay 位置 −（代偏移 + RelTime）；稳定 = 缓冲延迟
          - ``rate``：ΔRelTime / ΔAirPlay；持续偏离 1.0 才是真正的时钟漂移

        真正的时钟漂移目前**只记录 warning，不自动重建**（需要重建的只有三类
        明确原因：真实 seek、渲染器链路真的断了、播放真的结束）。
        """
        rel_time = self.timeline.renderer_rel_time_ms
        position = self.timeline.position_ms()
        if rel_time is None or position is None:
            return
        now = time.monotonic()
        offset = position - (self.timeline.generation_offset_ms + rel_time)
        previous = self._rel_sample
        if previous is not None:
            delta_air = position - previous[0]
            delta_rel = rel_time - previous[1]
            # 至少 1 秒的间隔才计算速率，避免采样噪声把速率算飞
            if delta_air >= 1000.0:
                self._rel_rate = delta_rel / delta_air
        self._rel_sample = (position, rel_time)
        self._rel_offset_ms = offset
        self._rel_observed_at = now
        if (self._rel_rate is not None
                and abs(self._rel_rate - 1.0) > self.RELTIME_RATE_TOLERANCE
                and now - self._rel_rate_warned_at >= self.RELTIME_RATE_WARN_INTERVAL):
            self._rel_rate_warned_at = now
            log.warning(
                "渲染器时钟速率偏离（ΔRelTime/ΔAirPlay=%.4f，位置偏差=%.0f ms）："
                "仅记录诊断，不重建会话",
                self._rel_rate, offset,
            )

    def _log_diagnostics(self) -> None:
        """周期性输出完整状态，便于真机分析（验收要求）。"""
        now = time.monotonic()
        if now - self._last_diag_log < self.DIAGNOSTIC_INTERVAL_SECONDS:
            return
        self._last_diag_log = now
        token = self._renderer_token
        session = self.streams.get(token) if token else None
        with self._lock:
            state_name = self.state.state
            renderer_state = self.state.renderer_state
        log.info(
            "诊断: airplay_pos=%s state=%s renderer=%s rel_time=%s offset=%s rate=%s "
            "gen=%s token=%s http_clients=%s bytes_served=%s last_range=%s "
            "idle=%.1fs uri_count=%d last_rebuild=%s awaiting_new_stream=%s",
            int(self.timeline.position_ms()) if self.timeline.position_ms() is not None else "-",
            state_name, renderer_state,
            int(self.timeline.renderer_rel_time_ms) if self.timeline.renderer_rel_time_ms is not None else "-",
            int(self._rel_offset_ms) if self._rel_offset_ms is not None else "-",
            "%.4f" % self._rel_rate if self._rel_rate is not None else "-",
            self.ring.generation, token[-6:] if token else "-",
            session.clients if session is not None else 0,
            int(session.bytes_served) if session is not None else 0,
            self._last_range_info or "-",
            (now - session.last_activity) if session is not None and session.last_activity else -1.0,
            self._uri_count, self._last_rebuild_reason or "-", self._awaiting_new_stream,
        )

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
                "diagnostics": {
                    "generation": self.ring.generation,
                    "renderer_rel_time_ms": self.timeline.renderer_rel_time_ms,
                    "rel_offset_ms": self._rel_offset_ms,
                    "rel_rate": self._rel_rate,
                    "uri_count": self._uri_count,
                    "uri_log": list(self._uri_log),
                    "last_rebuild_reason": self._last_rebuild_reason,
                    "awaiting_new_stream": self._awaiting_new_stream,
                    "transition_reason": self._transition_reason,
                },
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
