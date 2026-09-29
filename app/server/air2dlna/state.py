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

class _ResumeTimeline:
    """一次「暂停恢复」的分段耗时记录（对应 GPT 需求 T0~T14）。

    恢复延迟必须分段测量才能定位瓶颈：是代码、是 HTTP 供给、还是渲染器内部预缓冲。
    每个标记只记第一次出现的时间（幂等），最后输出一份可直接对照的报告。
    """

    #: 关键字节里程碑（字节 -> 标记名）
    BYTE_MARKS = ((100 * 1024, "T10_100kb"), (500 * 1024, "T11_500kb"),
                  (1024 * 1024, "T12_1mb"), (2 * 1024 * 1024, "T12b_2mb"),
                  (4 * 1024 * 1024, "T12c_4mb"))

    def __init__(self, byte_rate: int, reason: str = "") -> None:
        self.t0 = time.monotonic()
        self.byte_rate = max(1, int(byte_rate))
        self.reason = reason
        self.marks: dict[str, float] = {}
        self.reported = False
        self.bytes_at: list[tuple[str, int]] = []

    def mark(self, name: str) -> None:
        """记录一个时间标记（只记第一次）。"""
        self.marks.setdefault(name, time.monotonic())

    def mark_bytes(self, total_bytes: int) -> None:
        """按累计发送字节数记录里程碑（跨过阈值时触发）。"""
        for threshold, name in self.BYTE_MARKS:
            if total_bytes >= threshold and name not in self.marks:
                self.marks[name] = time.monotonic()
                self.bytes_at.append((name, total_bytes))

    def elapsed(self, name: str) -> Optional[float]:
        started = self.marks.get(name)
        if started is None:
            return None
        return (started - self.t0) * 1000.0

    def segment(self, start: str, end: str) -> Optional[float]:
        a, b = self.marks.get(start), self.marks.get(end)
        if a is None or b is None:
            return None
        return (b - a) * 1000.0

    def pcm_seconds(self, total_bytes: int) -> float:
        return total_bytes / float(self.byte_rate)

    def report(self, total_bytes: int = 0, clients: int = 0, generation: int = 0,
               range_info: str = "", renderer_state: str = "", rel_time_ms: Optional[float] = None) -> str:
        """输出可直接对照的分段报告（GPT 需求第 9 条）。"""

        def fmt(value: Optional[float]) -> str:
            return "n/a" if value is None else "%.0f ms" % value

        total = self.elapsed("T14_playing_confirmed") or self.elapsed("T13_reltime_moving")
        lines = [
            "=== 暂停恢复耗时报告 (%s) ===" % (self.reason or "resume"),
            "总耗时 T0→%s = %s" % (
                "T14" if "T14_playing_confirmed" in self.marks else "T13", fmt(total)),
            "  代码处理  T0→T5   = %s %s" % (
                fmt(self.segment("T0_airplay_resume", "T5_seturi_sent")),
                "(含等待渲染器重连 %s / fallback 决策 %s)" % (
                    fmt(self.segment("T1_recovery_begin", "T2_fallback_decided")),
                    fmt(self.segment("T2_fallback_decided", "T3_flush_done")))),
            "  换代+T3→T4       = %s" % fmt(self.segment("T3_flush_done", "T4_generation_done")),
            "  SetURI 往返 T5→T7= %s" % fmt(self.segment("T5_seturi_sent", "T7_play_sent")),
            "  HTTP 建连 T7→T8  = %s" % fmt(self.segment("T7_play_sent", "T8_http_get")),
            "  首包     T8→T9   = %s" % fmt(self.segment("T8_http_get", "T9_first_data")),
            "  拉取     T9→T13  = %s" % fmt(self.segment("T9_first_data", "T13_reltime_moving")),
            "  渲染器内部等待   = %s" % fmt(self.segment("T13_reltime_moving", "T14_playing_confirmed")),
            "  HTTP 累计 %.0f KB = %.1fs PCM | clients=%d gen=%d | %s | renderer=%s rel_time=%s" % (
                total_bytes / 1024.0, self.pcm_seconds(total_bytes), clients, generation,
                range_info or "-", renderer_state or "-",
                int(rel_time_ms) if rel_time_ms is not None else "-"),
        ]
        for name, value in self.bytes_at:
            lines.append("    里程碑 %s: 累计 %.0f KB (%.1fs PCM) @ %.0f ms" % (
                name.split("_", 1)[1], value / 1024.0, self.pcm_seconds(value),
                (self.marks[name] - self.t0) * 1000.0))
        return "\n".join(lines)


@dataclass
class _Intent:
    """收敛线程要达成的目标。"""

    mode: str = _MODE_STOP
    token: str = ""          # 要播放的流 token
    volume: Optional[int] = None
    revision: int = 0
    #: 是否在 SetAVTransportURI 之后立即发送 Play（prewarm 预热时置 False）
    play: bool = True
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
    #: keepalive 模式下暂停时的位置采样间隔（秒）：保持时间线快照
    KEEPALIVE_POSITION_SAMPLE_SECONDS = 1.0
    #: 暂停恢复时允许「暂停位置」领先已写入数据的最大秒数。
    #: AirPlay 上报的位置必然略微领先 FIFO 已写入的数据（真机约 10 毫秒），
    #: 但领先过多说明位置本身不可信（或数据早已被覆盖），此时不该启用方案 A。
    PAUSE_RECOVERY_LEAD_ALLOWANCE_SECONDS = 1.0
    #: 方案 A 启动后，若渲染器在这段时间内没有发起任何新的 HTTP 连接，
    #: 说明这台固件不会自己重连 —— 立刻 fallback。
    #: 取 1 秒：会自行重连的固件在恢复播放后通常几十毫秒内就发起连接。
    PAUSE_RECOVERY_RECONNECT_GRACE_SECONDS = 1.0
    #: 「暂停恢复」（方案 A）等待渲染器自己重连 HTTP 的窗口（秒）。
    #: 真机实测小爱音箱把 UPnP Pause 执行成 Stop，Stop 后只发 Play 会"假播放"
    #: （报告 PLAYING、继续拉流、但无声），必须靠它自己重新 GET（Range: bytes=0-）
    #: 重新建立音频管线。窗口内把「逻辑 0 点」映射到暂停位置即可有声且不掉位置；
    #: 窗口内没等到就 fallback 换代重建，避免卡死。
    PAUSE_RECOVERY_TIMEOUT_SECONDS = 5.0
    #: 恢复窗口内的快速轮询间隔（秒）。这段时间只做纯本地状态判定，
    #: 不发任何 SOAP，所以可以远快于常规轮询 —— 用户点恢复后要尽快出结论。
    PAUSE_RECOVERY_POLL_INTERVAL_SECONDS = 0.2
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
        #: 暂停恢复（方案 A）：暂停位置及其在当代流内的字节偏移
        self._pause_recovery_pending = False
        self._pause_recovery_deadline = 0.0
        self._paused_airplay_position_ms: Optional[float] = None
        self._paused_ring_offset: Optional[int] = None
        self._paused_generation = 0
        self._paused_at = 0.0
        self._pause_fallback_reason = ""
        #: 方案 A 启动时的 HTTP 连接计数基线（用于判断渲染器是否真的重连了）
        self._recovery_range_baseline = 0
        self._recovery_started_at = 0.0
        #: Renderer Profile（按 UDN）：记录该设备是否能可靠保持 keepalive。
        #: None/缺失 = 未验证；False = 已验证不可用（下次暂停直接走 current）
        self._keepalive_capable: dict[str, bool] = {}
        #: keepalive 期间连续没有客户端的采样次数（用于判定断流）
        self._keepalive_idle_ticks = 0
        #: keepalive 模式：暂停期间渲染器保持 PLAYING，输出层改送静音
        self._silence_active = False
        self._silence_started_at = 0.0
        self._silence_generation = 0
        #: 一次暂停恢复的分段耗时记录（GPT 需求 T0~T14）
        self._resume_timeline: Optional[_ResumeTimeline] = None
        #: 渲染器是否表现出「Stop 后会自己重连 HTTP」的能力。
        #: None = 未知（首次给一个短窗口验证）；False = 已确认不重连 → 下次直接 fallback。
        self._renderer_reconnect_capable: Optional[bool] = None
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
        if is_resume and self._silence_active:
            # keepalive(方案A) 的恢复：渲染器一直在 PLAYING、URI 从未改变，
            # 因此**不做任何 UPnP 操作**，只把输出层切回真实 PCM 即可。
            self._resume_timeline = _ResumeTimeline(self.ring.byte_rate, "keepalive")
            self._resume_timeline.mark("T0_airplay_resume")
            self._stop_pause_keepalive("恢复播放")
            self.timeline.on_resume()
            with self._lock:
                self.state.state = PLAYING
            self._resume_timeline.mark("T2_fallback_decided")
            log.info("keepalive(方案A): 恢复播放 —— 未换代、未 SetURI、未发送任何 UPnP 命令")
            return
        # T0：AirPlay 的 pres/pbeg 到达 —— 一次恢复的计时起点
        self._resume_timeline = _ResumeTimeline(self.ring.byte_rate,
                                                "resume" if is_resume else "pbeg")
        self._resume_timeline.mark("T0_airplay_resume")
        if is_resume:
            self._resume_timeline.mark("T1_recovery_begin")
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
            elif token and self._start_pause_recovery():
                # 方案 A 已启动：等待渲染器自己重连（Range 0 会被映射到暂停位置）
                if self._resume_timeline is not None:
                    self._resume_timeline.mark("T2_fallback_decided")
                return
            elif token and self._pause_recovery_pending:
                # 有待恢复的暂停，但方案 A 不适用（位置已被缓冲覆盖 / 会话不匹配）。
                # 此时**必须**走 fallback 换代 —— 因为渲染器已 STOPPED，只发 Play
                # 只会"假播放"（报告 PLAYING、继续拉流、但没有声音）。
                log.info("恢复播放：方案 A 不适用（%s），fallback 换代重建"
                         "（新 generation 的 0 点 = 当前 AirPlay 位置）",
                         self._pause_fallback_reason or "未知")
                if self._resume_timeline is not None:
                    self._resume_timeline.reason = "fallback:" + (
                        self._pause_fallback_reason or "unknown")
                    self._resume_timeline.mark("T2_fallback_decided")
                self._clear_pause_recovery("pause-recovery-unsupported")
                self._begin_new_generation(reason="pause-recovery-unsupported", offset_ms=None)
                self._start_dlna_session(reason="pause-recovery-unsupported")
            elif token and self._can_resume_in_place():
                # 渲染器确实处于暂停且位置连续 → 只发 Play 续播，
                # **绝不重设 URI**（重设会让它从资源 0 点重播）。
                log.info("恢复播放：渲染器处于暂停且位置连续，只发送 Play（沿用 gen=%d，不重设 URI）",
                         self.ring.generation)
                self._set_intent(_MODE_PLAY, token, set_uri=False)
            elif token:
                # 其它情况（渲染器其实已 STOPPED、或位置跳变/seek）：
                # 只发 Play 会"假播放"（无声），必须换代重建。
                log.info("恢复播放：渲染器状态=%s 不满足原地续播（或位置跳变），"
                         "换代重建（新 generation 的 0 点 = 当前 AirPlay 位置）",
                         self.state.renderer_state)
                self._begin_new_generation(reason="resume-rebuild", offset_ms=None)
                self._start_dlna_session(reason="resume-rebuild")
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

    def _can_resume_in_place(self) -> bool:
        """能否原地续播（只发 Play、不换代、不重设 URI）。

        必须同时满足：

        1. **渲染器确实处于 PAUSED** —— 真机上固件把 ``Pause`` 做成 ``Stop``，
           此时只发 ``Play`` 会"假播放"（报告 PLAYING、继续拉流、但无声）。
        2. **曲目位置连续** —— 位置跳变意味着用户 seek 到了别处，必须换代重建。

        真机事故：快速「暂停→恢复」或拖动进度条时，轮询还没来得及发现渲染器
        已 STOPPED，旧逻辑只看"有 token"就原地续播 → 无声、且再也恢复不了。
        """
        if self.state.renderer_state != _RENDERER_PAUSED:
            return False
        current = self.timeline.position_ms()
        baseline = self._position_at_stream_boundary
        if current is not None and baseline is not None:
            delta = abs(current - baseline)
            if delta > self.RESUME_POSITION_TOLERANCE_MS:
                log.info("恢复播放：曲目位置跳变 %.0f ms（可能是 seek），不能原地续播", delta)
                return False
        return True

    def _recovery_mode(self) -> str:
        """暂停恢复策略（实验开关）：current / keepalive / prewarm / auto。"""
        mode = str(self.config.get("recovery_mode") or "current").strip().lower()
        return mode if mode in ("current", "keepalive", "prewarm", "auto") else "current"

    # ---------------------------------------------------------- Pause Keepalive
    def _start_pause_keepalive(self, position: Optional[float], offset: Optional[int]) -> bool:
        """方案 A：暂停时不碰渲染器，由 HTTP 输出层改送静音。

        真机问题：固件把 UPnP ``Pause`` 做成 ``Stop``，恢复时必须
        ``SetAVTransportURI + Play`` 重建音频管线 —— 音箱重新预缓冲，等待 2~4 秒。
        本方案让渲染器**一直保持 PLAYING**：暂停期间服务端按实时速率送出静音 PCM
        （静音在**输出层**生成，绝不写入环形缓冲，AirPlay 时间线保持真实），
        恢复时直接切回真实 PCM —— 无需任何 UPnP 操作，理论上立即出声。
        """
        with self._lock:
            token = self._gen_token
        session = self.streams.get(token) if token else None
        if session is None or session.closed:
            log.info("keepalive: 当前没有可用的流会话，退回 current 方案")
            return False
        self._silence_active = True
        self._silence_started_at = time.monotonic()
        self._keepalive_idle_ticks = 0
        self._silence_generation = self.ring.generation
        session.silence_mode = True
        with self._lock:
            self.state.state = PAUSED
        log.info(
            "keepalive(方案A): 暂停但不操作渲染器（保持 PLAYING），"
            "HTTP 输出层改为发送静音 gen=%d 暂停位置=%s ms 偏移=%s 超时=%.0fs",
            self.ring.generation,
            int(position) if position is not None else "-",
            offset if offset is not None else "-",
            self._keepalive_timeout_seconds())
        return True

    def _stop_pause_keepalive(self, reason: str) -> None:
        """结束 keepalive：输出层切回真实 PCM。"""
        if not self._silence_active:
            return
        with self._lock:
            token = self._gen_token
        session = self.streams.get(token) if token else None
        if session is not None:
            session.silence_mode = False
        elapsed = time.monotonic() - self._silence_started_at
        self._silence_active = False
        log.info("keepalive(方案A): 结束静音（%s），输出层切回真实 PCM（保持 %.1fs，"
                 "期间未对渲染器做任何 UPnP 操作）", reason, elapsed)

    def _keepalive_udn(self) -> str:
        record = self.registry.selected()
        return record.udn if record is not None else ""

    def _keepalive_allowed(self) -> bool:
        """该渲染器是否还有资格尝试 keepalive（Renderer Profile）。"""
        return self._keepalive_capable.get(self._keepalive_udn(), True) is not False

    def _mark_keepalive_unsupported(self, reason: str) -> None:
        udn = self._keepalive_udn()
        if self._keepalive_capable.get(udn) is False:
            return
        self._keepalive_capable[udn] = False
        log.warning("keepalive: 标记渲染器 %s 不支持 keepalive（%s）——"
                    "后续暂停将直接使用 current 方案", udn or "?", reason)

    def _check_keepalive_health(self) -> None:
        """keepalive 期间的断流检测（GPT 需求 Phase 2）。

        keepalive 依赖渲染器持续保持 HTTP 连接与播放状态。若它自行断开、
        停止拉流或转入 STOPPED，就说明该设备不能长期保持 —— 立即结束 keepalive
        并记入 profile，下一次暂停直接走稳定的 current 方案。
        """
        if not self._silence_active:
            return
        with self._lock:
            token = self._gen_token
            renderer_state = self.state.renderer_state
        session = self.streams.get(token) if token else None
        if session is None or session.closed:
            self._mark_keepalive_unsupported("流会话已失效")
            self._stop_pause_keepalive("流会话失效")
            return
        if renderer_state == _RENDERER_STOPPED:
            self._mark_keepalive_unsupported("渲染器在暂停期间转入 STOPPED")
            self._stop_pause_keepalive("渲染器已停止")
            self._set_intent(_MODE_PAUSE, token)
            return
        if session.clients <= 0:
            self._keepalive_idle_ticks += 1
            if self._keepalive_idle_ticks >= 3:      # 约 3 个轮询周期（≈9s）
                self._mark_keepalive_unsupported("渲染器停止拉流")
                self._stop_pause_keepalive("渲染器停止拉流")
                self._set_intent(_MODE_PAUSE, token)
            return
        self._keepalive_idle_ticks = 0
        # 连接仍在、渲染器仍在播放 → 该设备可保持 keepalive
        self._keepalive_capable.setdefault(self._keepalive_udn(), True)

    def _keepalive_timeout_seconds(self) -> float:
        try:
            value = float(self.config.get("pause_keepalive_timeout_seconds"))
        except (TypeError, ValueError):
            return 30.0
        return max(5.0, min(3600.0, value))

    def _check_keepalive_timeout(self) -> None:
        """keepalive 超时 → 退化为 current（真正 Pause 渲染器，恢复时走可靠路径）。"""
        if not self._silence_active:
            return
        held = time.monotonic() - self._silence_started_at
        if held < self._keepalive_timeout_seconds():
            return
        log.info("keepalive(方案A): 已保持 %.0fs（超过 %.0fs），退化为 current 方案",
                 held, self._keepalive_timeout_seconds())
        self._stop_pause_keepalive("超时退化")
        # 真正暂停渲染器：之后恢复会走稳定的「换代 + SetURI + Play」
        self._set_intent(_MODE_PAUSE, self._gen_token)

    def _position_to_ring_offset(self, position_ms: Optional[float]) -> Optional[int]:
        """把「曲目位置」换算成当代流内的字节偏移（本代第 0 字节 = 代偏移）。"""
        if position_ms is None:
            return None
        try:
            base = self.timeline.generation_offset_ms
            byte_rate = self.ring.byte_rate
        except Exception:  # noqa: BLE001
            return None
        delta_ms = position_ms - base
        if delta_ms <= 0:
            return 0
        return int(delta_ms / 1000.0 * byte_rate)

    def _ring_window_text(self) -> str:
        """环形缓冲当前可用的数据窗口（诊断/日志用）。"""
        try:
            byte_rate = float(self.ring.byte_rate or 1)
            newest = self.ring.write_offset
            capacity_bytes = int(self.ring.capacity_seconds * self.ring.byte_rate)
            oldest = max(0, newest - capacity_bytes)
        except Exception:  # noqa: BLE001
            return "ring=?"
        return "ring=[%.1fs,%.1fs]" % (oldest / byte_rate, newest / byte_rate)

    def _pause_position_available(self) -> bool:
        """暂停位置的数据是否还在环形缓冲里（已被覆盖就只能 fallback 换代）。

        注意：**不能**要求 ``offset <= write_offset``。AirPlay 上报的播放位置必然
        略微领先于已写入 FIFO 的数据（真机实测差约 10 毫秒 / 1880 字节），严格比较
        会把本来可用的暂停恢复误判成"超出窗口"（1.0.10 的 bug）。
        略微领先没有影响 —— 取用最新的数据即可，见 ``_start_pause_recovery`` 的 clamp。
        """
        with self._lock:
            offset = self._paused_ring_offset
        if offset is None:
            return False
        try:
            byte_rate = self.ring.byte_rate
            newest = self.ring.write_offset
            oldest = max(0, newest - int(self.ring.capacity_seconds * byte_rate))
        except Exception:  # noqa: BLE001
            return False
        allowance = int(byte_rate * self.PAUSE_RECOVERY_LEAD_ALLOWANCE_SECONDS)
        return oldest <= offset <= newest + allowance

    def _start_pause_recovery(self) -> bool:
        """尝试「方案 A」：保持 generation / URI 不变，让渲染器自己重连 HTTP。

        真机实测：小爱音箱把 UPnP ``Pause`` 执行成了 ``Stop``，而 ``Stop`` 后只发
        ``Play`` 会产生"假播放"（报告 PLAYING、继续拉流、但无声）；只有
        ``SetAVTransportURI`` 能让音频管线重新激活，代价是从资源 0 点重播。
        本方案利用它 ``Stop`` 后会**自己重连 HTTP 并请求 ``Range: bytes=0-``** 的行为：
        恢复期间服务端把「逻辑 0 点」映射到暂停位置 —— 它以为自己从头播，
        实际听到的正是暂停位置之后的音频。

        返回 True 表示方案 A 已启动（调用方不要再换代/重设 URI）。
        """
        with self._lock:
            pending = self._pause_recovery_pending
            token = self._gen_token
            offset = self._paused_ring_offset
            position = self._paused_airplay_position_ms
            generation = self._paused_generation
        if not pending or not token or offset is None:
            return False
        session = self.streams.get(token)
        if session is None or session.closed or session.generation != generation:
            self._pause_fallback_reason = "会话缺失或已换代"
            log.info("暂停恢复: 当前会话不可用（%s），不启用方案 A", self._pause_fallback_reason)
            return False
        if not self._pause_position_available():
            self._pause_fallback_reason = "暂停位置超出缓冲窗口"
            log.info("暂停恢复: 暂停位置已超出环形缓冲窗口（%s），fallback 换代重建",
                     self._ring_window_text())
            return False

        try:
            newest = self.ring.write_offset
            if offset > newest:
                # 暂停位置比已写入数据领先几十毫秒 → 退回最新数据处
                log.info("暂停恢复: 暂停偏移 %d 字节略领先已写入数据 %d 字节，"
                         "按最新数据处映射", offset, newest)
                offset = newest
        except Exception:  # noqa: BLE001
            pass
        session.recovery_byte_offset = offset
        session.recovery_applied = False
        with self._lock:
            self._recovery_range_baseline = session.range_requests
            self._recovery_started_at = time.monotonic()
            self._pause_recovery_deadline = time.monotonic() + self.PAUSE_RECOVERY_TIMEOUT_SECONDS
        log.info(
            "暂停恢复: 启动方案 A（不换代、不换 URI、不 SetAVTransportURI）"
            "gen=%d paused_pos=%s ms 映射偏移=%d 字节 %s 超时=%.1fs",
            generation, int(position) if position is not None else "-",
            offset, self._ring_window_text(), self.PAUSE_RECOVERY_TIMEOUT_SECONDS)
        # 只发 Play：让渲染器从 STOPPED 回到 PLAYING 并自行重连 HTTP
        self._set_intent(_MODE_PLAY, token, set_uri=False)
        return True

    def _check_pause_recovery(self) -> None:
        """方案 A 收尾：成功判定 / 超时 fallback。"""
        with self._lock:
            deadline = self._pause_recovery_deadline
            if deadline <= 0.0:
                return
            token = self._gen_token
        session = self.streams.get(token) if token else None
        if session is not None and session.recovery_applied and session.clients > 0:
            session.recovery_byte_offset = None
            with self._lock:
                self._pause_recovery_deadline = 0.0
                self._pause_recovery_pending = False
            self._renderer_reconnect_capable = True
            log.info("暂停恢复: 成功 —— 渲染器已使用 Range 0 映射并在拉流，"
                     "位置即暂停位置，未换代、未换 URI（耗时 %.1fs）",
                     time.monotonic() - self._recovery_started_at)
            return
        now = time.monotonic()
        # 渲染器若会自己重连，通常在恢复播放后立刻发起。宽限期内一次新连接都没有，
        # 就说明这台固件不重连 —— 提前 fallback，避免用户白等整个超时窗口。
        grace = self._recovery_grace_seconds()
        if (session is not None and not session.recovery_applied
                and session.range_requests <= self._recovery_range_baseline
                and now - self._recovery_started_at >= grace):
            self._pause_fallback_reason = "渲染器未重连（固件不自行重建 HTTP）"
            if self._renderer_reconnect_capable is None:
                # 记录结论：下次恢复不再白等
                self._renderer_reconnect_capable = False
                log.info("暂停恢复: 确认该渲染器不会自行重连 HTTP，"
                         "后续恢复将直接 fallback（不再等待）")
            log.info("暂停恢复: 宽限期 %.1fs 内渲染器没有发起新的 HTTP 连接（%s），"
                     "fallback 换代重建，耗时 %.1fs",
                     grace, self._pause_fallback_reason,
                     now - self._recovery_started_at)
            with self._lock:
                self._pause_recovery_deadline = 0.0
                self._pause_recovery_pending = False
            session.recovery_byte_offset = None
            self._begin_new_generation(reason="pause-recovery-unavailable", offset_ms=None)
            self._start_dlna_session(reason="pause-recovery-unavailable")
            return
        if now < deadline:
            return
        with self._lock:
            self._pause_recovery_deadline = 0.0
            self._pause_recovery_pending = False
            self._pause_fallback_reason = "等待渲染器重连超时"
        log.warning(
            "暂停恢复: 超时（%.1fs 内渲染器没有使用 Range 0 映射），fallback 换代重建 "
            "—— 新 generation 的 0 点 = 当前 AirPlay 位置，不会回退到旧位置",
            self.PAUSE_RECOVERY_TIMEOUT_SECONDS)
        if session is not None:
            session.recovery_byte_offset = None
        self._begin_new_generation(reason="pause-recovery-fallback", offset_ms=None)
        self._start_dlna_session(reason="pause-recovery-fallback")

    def _clear_pause_recovery(self, reason: str) -> None:
        """清除暂停恢复状态（seek / 播放结束等场景）。"""
        with self._lock:
            had = self._pause_recovery_pending or self._pause_recovery_deadline > 0.0
            self._pause_recovery_pending = False
            self._pause_recovery_deadline = 0.0
            self._paused_ring_offset = None
            self._paused_airplay_position_ms = None
        if had:
            log.debug("清除暂停恢复状态（%s）", reason)

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
        position = self.timeline.position_ms()
        offset = self._position_to_ring_offset(position)
        with self._lock:
            self._position_at_stream_boundary = position
            self._paused_airplay_position_ms = position
            self._paused_ring_offset = offset
            self._paused_generation = self.ring.generation
            self._paused_at = time.monotonic()
        log.info(
            "AirPlay 暂停：向渲染器发送 Pause（沿用当前 DLNA 会话 gen=%d）"
            "；暂停恢复信息 airplay_pos=%s ms ring_offset=%s 字节 %s",
            self.ring.generation,
            int(position) if position is not None else "-",
            offset if offset is not None else "-", self._ring_window_text())
        mode = self._recovery_mode()
        if mode in ("keepalive", "auto") and self._keepalive_allowed():
            pass
        elif mode in ("keepalive", "auto"):
            log.info("keepalive: 该渲染器已被标记为不支持 keepalive（断流/超时过），"
                     "本次直接使用 current 方案")
            mode = "current"
        if mode in ("keepalive", "auto"):
            # 方案 A：不调用 UPnP Pause（渲染器保持 PLAYING），输出层送静音
            if self._start_pause_keepalive(position, offset):
                self.timeline.on_pause()
                return
        self.timeline.on_pause()
        with self._lock:
            self.state.state = PAUSED
        self._set_intent(_MODE_PAUSE, self._gen_token)
        if mode == "prewarm":
            # 方案 B：暂停期间就把下一代媒体准备好（换代 + SetURI，不 Play），
            # 让渲染器提前建连并预缓冲；恢复时只需一个 Play。
            log.info("prewarm(方案B): 暂停后预热下一代流（换代 + SetAVTransportURI，不 Play）")
            self._begin_new_generation(reason="prewarm", offset_ms=None)
            self._start_dlna_session(reason="prewarm", play=False)

    def _handle_flush(self, payload: str) -> None:
        """``pfls`` / ``pdis``：真实 seek。载荷是要 flush 到的帧号。

        这是**唯一**由 AirPlay 侧驱动的媒体生命周期变更（需求 A 类）：
        flush 旧 PCM → 新 generation → 新 token/URI → SetAVTransportURI → Play。
        """
        self._cancel_transition("seek/flush")
        self._stop_pause_keepalive("seek")
        self._clear_pause_recovery("seek")
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
        self._stop_pause_keepalive("播放结束")
        self._clear_pause_recovery("播放结束")
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
        if self._resume_timeline is not None:
            self._resume_timeline.mark("T3_flush_done")
        # 可选：恢复时先填一小段静音，让渲染器一连上就有数据可读
        #（A/B 测试用；默认 0 = 保持原行为，见 config.resume_prebuffer_ms）
        prebuffer_ms = int(self.config.get("resume_prebuffer_ms") or 0)
        if prebuffer_ms > 0:
            try:
                byte_rate = self.ring.byte_rate
                frames = int(byte_rate * prebuffer_ms / 1000.0) & ~0x3
                if frames > 0:
                    self.ring.append(b"\x00" * frames)
                    log.info("恢复预填充: 写入 %d ms 静音（%d 字节）供渲染器立即读取",
                             prebuffer_ms, frames)
            except Exception:  # noqa: BLE001
                log.debug("恢复预填充失败", exc_info=True)
        kind, _content_type = "wav", "audio/wav"
        with self._lock:
            self.state.duration_ms = None
            self.state.position_ms = None
            self.state.state = BUFFERING
        self.timeline.begin_generation(generation, offset_ms)
        if self._resume_timeline is not None:
            self._resume_timeline.mark("T4_generation_done")
        log.info("音频缓冲换代: gen=%d 原因=%s", generation, reason)

    # ------------------------------------------------------------- 意图下发
    def _set_intent(self, mode: str, token: str, volume: Optional[int] = None,
                    set_uri: bool = True, play: bool = True) -> None:
        with self._intent_lock:
            self._intent.revision += 1
            self._intent.mode = mode
            self._intent.set_uri = set_uri
            self._intent.play = play
            if token:
                self._intent.token = token
            if volume is not None:
                self._intent.volume = volume
        self._intent_event.set()

    def _start_dlna_session(self, reason: str, play: bool = True) -> None:
        """创建新一代流并请求收敛线程去播放它（``play=False`` 用于预热）。"""
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
        session.on_bytes = self._note_resume_bytes
        session.on_connect = self._note_resume_connect
        with self._lock:
            self._gen_token = session.token
            self._paused_with_stop = False
        self._set_intent(_MODE_PLAY, session.token, play=play)

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
                    play=self._intent.play,
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
            self._do_play(record, client, intent.token, set_uri=intent.set_uri,
                          play=intent.play)

    def _do_play(self, record, client, token: str, set_uri: bool = True,
                 play: bool = True) -> None:
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
        if self._resume_timeline is not None:
            self._resume_timeline.mark("T5_seturi_sent")
        if not self._safe_call(record, client.set_av_transport_uri, "SetAVTransportURI",
                               uri=uri, metadata=metadata):
            return
        if self._resume_timeline is not None:
            self._resume_timeline.mark("T6_seturi_returned")
        if not play:
            # prewarm（方案 B）：只把新 URI 交给渲染器并让它自行建连/预缓冲，
            # 不发 Play —— 避免暂停期间漏出声音。恢复时只补一个 Play。
            log.info("prewarm(方案B): 已 SetAVTransportURI 但不 Play，等待用户恢复时再播放")
            with self._lock:
                self.state.state = PAUSED
            return
        # 部分渲染器在 SetAVTransportURI 之后需要短暂准备
        time.sleep(0.3)
        if self._resume_timeline is not None:
            self._resume_timeline.mark("T7_play_sent")
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
                # 暂停恢复（方案 A）的成功判定 / 超时 fallback
                self._check_pause_recovery()
                # keepalive（方案 A'）断流检测与超时退化（GPT Phase 2）
                self._check_keepalive_health()
                self._check_keepalive_timeout()
                self._log_diagnostics()
            except Exception:  # noqa: BLE001
                log.exception("位置轮询异常")
            # 恢复窗口期间改用 0.2 秒级的纯本地判定：把「用户点恢复 → 出结论」
            # 的等待压到最短（常规轮询间隔是 3 秒级，会把恢复拖长好几秒）。
            if self._pause_recovery_deadline > 0.0:
                self._wait_recovery_window()
                continue
            # 加 ±20% 抖动，避免与设备自身的定时任务共振
            import random

            self._stop_event.wait(interval * random.uniform(0.8, 1.2))

    def _wait_recovery_window(self) -> None:
        """恢复窗口内的快速本地轮询（只做状态判定，不发 SOAP）。"""
        steps = int(self.PAUSE_RECOVERY_TIMEOUT_SECONDS
                    / self.PAUSE_RECOVERY_POLL_INTERVAL_SECONDS) + 5
        for _ in range(steps):
            if self._stop_event.wait(self.PAUSE_RECOVERY_POLL_INTERVAL_SECONDS):
                return
            try:
                self._check_pause_recovery()
            except Exception:  # noqa: BLE001
                log.exception("暂停恢复判定异常")
                return
            if self._pause_recovery_deadline <= 0.0:
                return

    def _recovery_grace_seconds(self) -> float:
        """等待渲染器自己重连的宽限期；已确认不重连的固件直接 0 等待。"""
        if self._renderer_reconnect_capable is False:
            return 0.0
        return self.PAUSE_RECOVERY_RECONNECT_GRACE_SECONDS

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

        # 渲染器把 Pause 实现成了 Stop（真机实测）：标记「待暂停恢复」，
        # Resume 时优先走方案 A（等它自己重连 HTTP，把逻辑 0 点映射到暂停位置）。
        if (renderer_state == _RENDERER_STOPPED
                and self.state.state == PAUSED
                and self._paused_ring_offset is not None
                and not self._pause_recovery_pending
                and self._paused_generation == self.ring.generation):
            with self._lock:
                self._pause_recovery_pending = True
            log.info("暂停恢复: 检测到渲染器实际为 STOPPED（固件把 Pause 实现成 Stop）"
                     "，已标记待恢复；Resume 时将优先尝试「重连 Range 0 → 映射到暂停位置」")

        if renderer_state in (_RENDERER_PLAYING, _RENDERER_PAUSED):
            try:
                position = client.get_position_info()
            except Exception as exc:  # noqa: BLE001
                log.debug("GetPositionInfo 失败: %s", exc)
                position = None
            if position:
                rel = position.get("rel_time_ms")
                if rel is not None and float(rel) > 0:
                    self._maybe_report_resume(self.streams.get(self._renderer_token), float(rel))
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
        if self._pause_recovery_pending:
            # 方案 A 进行中：渲染器可能刚 STOPPED、正准备重连，绝不能被兜底重建打断
            return
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

    def _note_resume_connect(self) -> None:
        """渲染器对新 URI 发起 HTTP GET 的回调（记录 T8）。"""
        timeline = self._resume_timeline
        if timeline is not None and not timeline.reported:
            timeline.mark("T8_http_get")

    def _note_resume_bytes(self, total_bytes: int) -> None:
        """HTTP 写出字节回调（由 stream.serve 调用）：记录首包时刻与字节里程碑。"""
        timeline = self._resume_timeline
        if timeline is None or timeline.reported:
            return
        timeline.mark("T9_first_data")
        timeline.mark_bytes(total_bytes)

    def _maybe_report_resume(self, session=None, rel_time_ms: Optional[float] = None) -> None:
        """满足确认条件（或超时）后输出一次恢复耗时报告（GPT 需求 T0~T14 / 第 9 条）。"""
        timeline = self._resume_timeline
        if timeline is None or timeline.reported:
            return
        now = time.monotonic()
        confirmed = False
        if rel_time_ms is not None and rel_time_ms > 0:
            timeline.mark("T13_reltime_moving")
        if session is not None:
            served = session.bytes_served
            moving = "T13_reltime_moving" in timeline.marks
            if session.clients > 0 and (moving or served >= 500 * 1024):
                timeline.mark("T14_playing_confirmed")
                confirmed = True
        timed_out = (now - timeline.t0) > 20.0
        if not confirmed and not timed_out:
            return
        timeline.reported = True
        with self._lock:
            renderer_state = self.state.renderer_state
        log.info(
            "%s\n%s",
            "暂停恢复耗时报告" if confirmed else "暂停恢复耗时报告（未确认出声，仅超时输出）",
            timeline.report(
                total_bytes=session.bytes_served if session is not None else 0,
                clients=session.clients if session is not None else 0,
                generation=session.generation if session is not None else self.ring.generation,
                range_info=session.last_range_info if session is not None else "",
                renderer_state=renderer_state, rel_time_ms=rel_time_ms),
        )

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
            "idle=%.1fs uri_count=%d last_rebuild=%s awaiting_new_stream=%s pause_recovery=%s "
            "keepalive_capable=%s",
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
            ("active" if self._pause_recovery_deadline > 0.0
             else ("armed" if self._pause_recovery_pending else "-")),
            self._keepalive_capable.get(self._keepalive_udn(), None),
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
