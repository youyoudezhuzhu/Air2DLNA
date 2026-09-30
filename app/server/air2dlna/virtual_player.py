"""Virtual Player：唯一状态协调中心。

对应 ARCHITECTURE_V2 第 1、2、3、10~20、22、23、26 节。

角色
----

::

    AirPlay（Playback Authority）
        ↓ 状态事件 + PCM
    Virtual Player（State Coordinator）   ← 本模块
        ↓ 意图（PLAY / PAUSE / SEEK / TRACK_CHANGED / STOP）
    DLNA Output（Output Renderer）        ← dlna_output / stream

原则：

* **AirPlay 是唯一播放状态权威**。本模块不自行创造「真实播放状态」；
  DLNA 端的 Play/Pause/Seek/Next/Previous 只是**控制请求**（pending request），
  必须反向作用到 AirPlay，等待 AirPlay 实际状态变化后再更新。
* 实际状态机（``IDLE / BUFFERING / PLAYING / PAUSED / SEEKING /
  TRACK_SWITCHING / RECOVERING / STOPPING``）与**请求状态**
  （``PLAY_REQUESTED / PAUSE_REQUESTED / SEEK_REQUESTED / NEXT_REQUESTED /
  PREVIOUS_REQUESTED``）严格分离，绝不混为一谈。
* 每个控制请求都携带 ``request_id`` 与 ``control_source``
  （``AIRPLAY`` / ``DLNA`` / ``INTERNAL``）；由 DLNA 发起、被 AirPlay 回显的
  状态变化不得再次触发同一条 DLNA 动作（防止控制回环）。
* 反向能力不可用（``UNSUPPORTED`` / ``UNKNOWN``）或调用失败时，
  **绝不假装状态已改变**，保持当前状态与当前曲目（第 20 节）。
* 现有 AirPlay Timeline / RingBuffer 保持原样：时间线只代表真实 AirPlay 音频，
  静音只在 DLNA Output 层生成（第 22、23 节）。
* Recovery（``RECOVERING`` → new generation + SetURI + Play）只是**后备路径**，
  不是正常 Resume / Seek / Pause 的主路径（第 26 节）。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .airplay_remote import (
    CAPABILITIES,
    SUPPORTED,
    UNKNOWN,
    UNSUPPORTED,
    AirPlayRemoteController,
)
from .dlna_output import (
    BUFFERING,
    ERROR,
    MODE_PAUSE,
    MODE_PLAY,
    MODE_STOP,
    PAUSED,
    PLAYING,
    RENDERER_PAUSED,
    RENDERER_PLAYING,
    RENDERER_STOPPED,
    STOPPED,
    DLNAOutput,
)
from .metadata import MetadataItem
from .ringbuffer import PcmRingBuffer
from .timeline import AudioTimeline

log = logging.getLogger("virtual_player")

# --------------------------------------------------------------------- 状态机
#: 实际播放状态（ARCHITECTURE_V2 第 3 节）
IDLE = "IDLE"
MACHINE_BUFFERING = "BUFFERING"
MACHINE_PLAYING = "PLAYING"
MACHINE_PAUSED = "PAUSED"
SEEKING = "SEEKING"
TRACK_SWITCHING = "TRACK_SWITCHING"
RECOVERING = "RECOVERING"
STOPPING = "STOPPING"

#: 独立的「请求状态」——绝不与实际状态混为一谈
PLAY_REQUESTED = "PLAY_REQUESTED"
PAUSE_REQUESTED = "PAUSE_REQUESTED"
SEEK_REQUESTED = "SEEK_REQUESTED"
NEXT_REQUESTED = "NEXT_REQUESTED"
PREVIOUS_REQUESTED = "PREVIOUS_REQUESTED"

#: 控制来源
SOURCE_AIRPLAY = "AIRPLAY"
SOURCE_DLNA = "DLNA"
SOURCE_INTERNAL = "INTERNAL"

_ACTION_TO_REQUEST = {
    "play": PLAY_REQUESTED,
    "pause": PAUSE_REQUESTED,
    "seek": SEEK_REQUESTED,
    "next": NEXT_REQUESTED,
    "previous": PREVIOUS_REQUESTED,
}

#: 实际状态 -> 对外暴露的 legacy 播放状态（兼容既有 UI / REST 契约）
_MACHINE_TO_LEGACY = {
    IDLE: STOPPED,
    MACHINE_BUFFERING: BUFFERING,
    MACHINE_PLAYING: PLAYING,
    MACHINE_PAUSED: PAUSED,
    SEEKING: BUFFERING,
    TRACK_SWITCHING: BUFFERING,
    RECOVERING: BUFFERING,
    STOPPING: STOPPED,
}
_LEGACY_TO_MACHINE = {
    STOPPED: IDLE,
    BUFFERING: MACHINE_BUFFERING,
    PLAYING: MACHINE_PLAYING,
    PAUSED: MACHINE_PAUSED,
    ERROR: IDLE,
}


@dataclass
class PlaybackState:
    """对外暴露的统一播放状态。

    ``state`` 保持既有的 legacy 取值（STOPPED/PAUSED/PLAYING/BUFFERING/ERROR），
    ``machine_state`` 是 Virtual Player 的真实状态机（IDLE/BUFFERING/PLAYING/
    PAUSED/SEEKING/TRACK_SWITCHING/RECOVERING/STOPPING），
    ``pending_request`` 是独立的请求状态。
    """

    state: str = STOPPED
    machine_state: str = IDLE
    pending_request: str = ""
    control_source: str = ""
    request_id: str = ""
    position_ms: Optional[float] = None
    duration_ms: Optional[float] = None
    title: str = ""
    artist: str = ""
    album: str = ""
    album_art: Optional[bytes] = None
    album_art_mime: str = "image/jpeg"
    volume: int = 0
    muted: bool = False
    renderer_state: str = RENDERER_STOPPED
    audio_format: str = ""
    airplay_session: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, artwork_url: Optional[str] = None) -> dict:
        return {
            "state": self.state,
            "machine_state": self.machine_state,
            "pending_request": self.pending_request,
            "control_source": self.control_source,
            "request_id": self.request_id,
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


class VirtualPlayer:
    """唯一状态协调中心：AirPlay 事件 → 状态 → DLNA Output 意图。"""

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
    #: 暂停期间累计收到多少音频（秒）就认定 AirPlay 已恢复播放（兜底，无事件也生效）。
    #: 取 0.5 秒：暂停时残留的尾部数据不会超过这个量。
    RESUMED_AUDIO_THRESHOLD_SECONDS = 0.25
    #: 暂停恢复时允许「暂停位置」领先已写入数据的最大秒数。
    #: AirPlay 上报的位置必然略微领先 FIFO 已写入的数据（真机约 10 毫秒），
    #: 但领先过多说明位置本身不可信（或数据早已被覆盖），此时不该启用方案 A。
    PAUSE_RECOVERY_LEAD_ALLOWANCE_SECONDS = 1.0
    #: 方案 A 启动后，若渲染器在这段时间内没有发起任何新的 HTTP 连接，
    #: 说明这台固件不会自己重连 —— 立刻 fallback。
    #: 取 1 秒：会自行重连的固件在恢复播放后通常几十毫秒内就发起连接。
    PAUSE_RECOVERY_RECONNECT_GRACE_SECONDS = 1.0
    #: 「暂停恢复」（方案 A）等待渲染器自己重连 HTTP 的窗口（秒）。
    PAUSE_RECOVERY_TIMEOUT_SECONDS = 5.0
    #: 恢复窗口内的快速轮询间隔（秒）。这段时间只做纯本地状态判定，
    #: 不发任何 SOAP，所以可以远快于常规轮询 —— 用户点恢复后要尽快出结论。
    PAUSE_RECOVERY_POLL_INTERVAL_SECONDS = 0.2
    #: 判定「渲染器时钟速率异常」的相对偏差容差（ΔRelTime / ΔAirPlay）。
    RELTIME_RATE_TOLERANCE = 0.20
    #: 速率告警的最小间隔（秒）：真漂移会持续存在，不必每条采样都记
    RELTIME_RATE_WARN_INTERVAL = 60.0
    #: ``pbeg`` 时判断「播放位置是否连续」的容差，比较的是**曲目位置本身的变化**。
    RESUME_POSITION_TOLERANCE_MS = 4000.0
    #: 刚刚处理过真实 seek（``pfls``/``pdis``）的窗口（秒）。
    SEEK_REUSE_WINDOW_SECONDS = 10.0

    def __init__(self, config, registry, ring: PcmRingBuffer, timeline: AudioTimeline,
                 streams, output: DLNAOutput, remote: AirPlayRemoteController,
                 lock: threading.RLock, stop_event: threading.Event,
                 state: Optional[PlaybackState] = None,
                 log: Optional[Callable[..., None]] = None) -> None:
        self.config = config
        self.registry = registry
        self.ring = ring
        self.timeline = timeline
        self.streams = streams
        self.output = output
        self.remote = remote
        self._lock = lock
        self._stop_event = stop_event
        self.log = log or (lambda msg, *a: None)

        #: 与 DLNA Output **共享**的唯一状态对象
        self.state = state if state is not None else PlaybackState()
        self.machine_state = IDLE

        #: 独立于实际状态的「控制请求」（第 3 / 19 节）
        self._pending_action = ""
        self._pending_source = ""
        self._pending_request_id = ""
        self._request_seq = 0

        self._poller: Optional[threading.Thread] = None
        #: 重建冷却（stalled 兜底 / 过渡超时等都用它，避免重建风暴）
        self._last_rebuild_at = 0.0
        #: 上次因「渲染器没在拉流」而重建的时刻
        self._last_stall_rebuild = 0.0
        self._last_rebuild_reason = ""
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
        self._keepalive_capable: dict[str, bool] = {}
        #: 是否已就「Profile 声明不支持暂停 → keepalive 不适用」记录过一次日志
        self._keepalive_profile_warned = False
        #: 暂停状态下累计收到的 PCM 字节数（用于「AirPlay 已恢复但没有事件」的兜底）
        self._paused_audio_bytes = 0
        #: keepalive 期间连续没有客户端的采样次数（用于判定断流）
        self._keepalive_idle_ticks = 0
        #: keepalive 模式：暂停期间渲染器保持 PLAYING，输出层改送静音
        self._silence_active = False
        self._silence_started_at = 0.0
        self._silence_generation = 0
        #: 一次暂停恢复的分段耗时记录（GPT 需求 T0~T14）
        self._resume_timeline: Optional[_ResumeTimeline] = None
        #: 渲染器是否表现出「Stop 后会自己重连 HTTP」的能力。
        self._renderer_reconnect_capable: Optional[bool] = None
        #: 最近一次因真实 seek 换代的时刻
        self._handled_seek_at = 0.0
        self._last_range_info = ""
        self._last_diag_log = 0.0
        self._session_started_at = 0.0
        self._pending_reanchor = False
        self._pre_flush_hook: Optional[Callable[[], None]] = None
        self._on_airplay_name_change: Optional[Callable[[str], None]] = None
        self._artwork_lock = threading.Lock()
        #: 反向控制统计（诊断）
        self._reverse_control_attempts = 0
        self._reverse_control_success = 0
        self._last_reverse_result: dict[str, Any] = {}

        # DLNA 输出层状态变化回调：把 legacy 状态映射回真实状态机
        self.output.on_state_change = self._on_output_state
        self.output.on_mark = self._mark_resume

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self.output.refresh_base_url()
        self.output.start()
        self._poller = threading.Thread(target=self._poll_loop, name="vlp-poller", daemon=True)
        self._poller.start()

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self.streams.close_all()
        except Exception:  # noqa: BLE001
            pass
        self.output.stop()
        if self._poller is not None and self._poller.is_alive():
            try:
                self._poller.join(timeout=5.0)
            except RuntimeError:
                pass

    def set_airplay_name_callback(self, callback: Callable[[str], None]) -> None:
        self._on_airplay_name_change = callback

    def set_pre_flush_hook(self, hook: Callable[[], None]) -> None:
        """注册「换代前」钩子。

        seek 时管道里可能仍残留 seek 之前的音频（pipe 后端不知道 flush 事件），
        必须先丢弃，否则旧音频会被写进新一代的开头。
        """
        self._pre_flush_hook = hook

    # ------------------------------------------------------------- 状态机
    def _enter(self, machine: str, legacy: Optional[str] = None, reason: str = "") -> None:
        """进入真实状态机的一个状态，并同步对外暴露的 legacy 状态。"""
        with self._lock:
            self.machine_state = machine
            self.state.machine_state = machine
            target = legacy if legacy is not None else _MACHINE_TO_LEGACY.get(machine)
            if target is not None:
                self.state.state = target
        if reason:
            log.debug("状态机: -> %s（%s）", machine, reason)

    def _on_output_state(self, legacy_state: str) -> None:
        """DLNA 输出层改变了 legacy 状态 —— 映射回真实状态机。"""
        machine = _LEGACY_TO_MACHINE.get(legacy_state)
        if machine is None:
            return
        with self._lock:
            self.machine_state = machine
            self.state.machine_state = machine

    # --------------------------------------------------------- 控制请求（pending）
    def _set_pending_request(self, action: str, source: str, request_id: str = "") -> str:
        with self._lock:
            self._request_seq += 1
            rid = request_id or "%s-%d-%d" % (source or SOURCE_INTERNAL,
                                              int(time.monotonic() * 1000), self._request_seq)
            self._pending_action = action
            self._pending_source = source or SOURCE_INTERNAL
            self._pending_request_id = rid
            self.state.pending_request = _ACTION_TO_REQUEST.get(action, "")
            self.state.control_source = self._pending_source
            self.state.request_id = rid
            return rid

    def _clear_pending_request(self, action: Optional[str] = None) -> bool:
        with self._lock:
            if action is not None and self._pending_action != action:
                return False
            self._pending_action = ""
            self._pending_source = ""
            self._pending_request_id = ""
            self.state.pending_request = ""
            self.state.control_source = ""
            self.state.request_id = ""
            return True

    def take_pending_request(self) -> dict[str, str]:
        """取出并清除当前请求（用于确认 / 诊断）。"""
        with self._lock:
            data = {
                "action": self._pending_action,
                "source": self._pending_source,
                "request_id": self._pending_request_id,
            }
            self._pending_action = ""
            self._pending_source = ""
            self._pending_request_id = ""
            self.state.pending_request = ""
            self.state.control_source = ""
            self.state.request_id = ""
            return data

    def pending_request(self) -> dict[str, str]:
        with self._lock:
            return {
                "action": self._pending_action,
                "request": _ACTION_TO_REQUEST.get(self._pending_action, ""),
                "source": self._pending_source,
                "request_id": self._pending_request_id,
            }

    # -------------------------------------------------------------- 反向控制
    def update_remote_credentials(self) -> None:
        """把 AirPlay 会话里的 DACP 凭据同步给反向控制器（如实检测）。"""
        with self._lock:
            session = dict(self.state.airplay_session)
        self.remote.update_from_session(session)

    def _reverse_control_enabled(self) -> bool:
        try:
            value = self.config.get("reverse_control_enabled")
        except Exception:  # noqa: BLE001
            return True
        return True if value is None else bool(value)

    def request_control(self, action: str, source: str = SOURCE_DLNA,
                        request_id: str = "", position_ms: Optional[int] = None) -> dict[str, Any]:
        """接收一个控制**请求**（DLNA / INTERNAL）。

        绝不直接改变播放状态：只有 AirPlay 实际状态变化（``paus``/``pres``/
        ``pfls`` 等事件）才更新状态。能力不可用或调用失败时保持当前状态与曲目。
        """
        action = (action or "").strip().lower()
        if action not in _ACTION_TO_REQUEST:
            return {"ok": False, "action": action, "capability": UNKNOWN,
                    "detail": "未知控制动作", "state_changed": False}
        with self._lock:
            if self._pending_action == action:
                return {"ok": False, "action": action, "pending": True,
                        "capability": self.remote.capability(action),
                        "detail": "同一控制请求尚未确认", "state_changed": False}
        capability = self.remote.capability(action)
        if not self._reverse_control_enabled():
            detail = "反向控制已在配置中关闭（reverse_control_enabled=false）"
            log.info("控制请求 %s（来源 %s）被拒绝：%s —— 保持当前状态与曲目",
                     action, source, detail)
            self._last_reverse_result = {
                "ok": False, "action": action, "capability": capability,
                "detail": detail, "state_changed": False,
            }
            return dict(self._last_reverse_result)
        if not self.remote.can_issue(action):
            # 凭据缺失 / 已被判不支持：如实报告，绝不假装状态改变（第 20 节）
            detail = ("DACP 凭据不完整（AirPlay 2 发送端实测不提供 daid/acre/dapo）"
                      if not self.remote.available
                      else "该能力此前已被实测判定为不支持")
            log.info("控制请求 %s（来源 %s）无法下发：%s（能力 %s）——保持当前状态与曲目",
                     action, source, detail, capability)
            self._last_reverse_result = {
                "ok": False, "action": action, "capability": capability,
                "detail": detail, "state_changed": False,
            }
            return dict(self._last_reverse_result)

        rid = self._set_pending_request(action, source, request_id)
        self._reverse_control_attempts += 1
        log.info("控制请求 %s（来源 %s, request_id=%s）→ AirPlay 反向控制",
                 action, source, rid)
        try:
            if action == "seek":
                result = self.remote.seek(int(position_ms or 0))
            else:
                result = getattr(self.remote, action)()
        except Exception as exc:  # noqa: BLE001 - 反向控制异常不得影响播放
            result = None
            log.exception("反向控制 %s 异常", action)
            detail = f"{type(exc).__name__}: {exc}"
        else:
            detail = result.detail

        if result is None or not result.ok:
            # 调用失败：清除请求，**保持当前状态与当前曲目**
            self._clear_pending_request(action)
            self._last_reverse_result = {
                "ok": False, "action": action,
                "capability": result.status if result is not None else UNKNOWN,
                "detail": detail, "request_id": rid, "state_changed": False,
            }
            log.warning("控制请求 %s 未成功（%s）：保持当前状态与曲目，不伪造变化",
                        action, detail or "未知原因")
            return dict(self._last_reverse_result)

        self._reverse_control_success += 1
        # 成功下发 ≠ 状态已改变：等待 AirPlay 事件确认
        self._last_reverse_result = {
            "ok": True, "action": action, "capability": result.status,
            "detail": result.detail, "request_id": rid,
            "awaiting_confirmation": True, "state_changed": False,
        }
        log.info("控制请求 %s 已下发（request_id=%s）；等待 AirPlay 实际状态确认",
                 action, rid)
        return dict(self._last_reverse_result)

    def _confirm_pending(self, action: str) -> Optional[dict[str, str]]:
        """AirPlay 事件确认了某个请求；返回被确认的请求信息（若来源是 DLNA）。"""
        with self._lock:
            if self._pending_action != action:
                return None
            data = {
                "action": self._pending_action,
                "source": self._pending_source,
                "request_id": self._pending_request_id,
            }
        self._clear_pending_request(action)
        log.info("控制请求已由 AirPlay 确认: %s（来源 %s, request_id=%s）",
                 data["action"], data["source"], data["request_id"])
        return data

    # ------------------------------------------------------------------ 音频入口
    def on_audio_bytes(self, data: bytes) -> None:
        """音频读取线程回调：把 FIFO 数据推进环形缓冲。"""
        self.ring.append(data)
        # 兜底：暂停期间持续收到 PCM，说明 AirPlay 其实已经恢复（真机实测拖动进度条时
        # AirPlay 可能**完全不发** pres/pbeg/pfls 任何事件），此时只靠事件永远醒不过来。
        # 这里只做计数，实际恢复动作交给轮询线程（不在音频线程里做网络操作）。
        if self.state.state == PAUSED:
            self._paused_audio_bytes += len(data)
        else:
            self._paused_audio_bytes = 0
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
            self.update_remote_credentials()
            log.info("AirPlay client connected (device=%r, ip=%s)",
                     self.state.airplay_session.get("name", ""), item.text)
        elif code == "disc":
            log.info("AirPlay 客户端断开: %s", item.text)
            self._handle_session_end("客户端断开")
        elif code == "snam":
            with self._lock:
                self.state.airplay_session["name"] = item.text
            log.info("AirPlay client device: %s", item.text)
            if self._on_airplay_name_change is not None:
                try:
                    self._on_airplay_name_change(item.text)
                except Exception:  # noqa: BLE001
                    log.debug("AirPlay 名称回调异常", exc_info=True)
        elif code == "snua":
            with self._lock:
                self.state.airplay_session["user_agent"] = item.text
        elif code == "svna":
            log.info("AirPlay 服务名已注册: %s", item.text)
        elif code == "styp":
            log.info("AirPlay 流类型: %s", item.text)
            with self._lock:
                self.state.airplay_session["stream_type"] = item.text
        elif code in ("daid", "acre", "dapo"):
            self._capture_dacp(code, item.text)
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
        elif code == "prsm":
            # prsm = play stream resume：AirPlay 明确告知"流已恢复/继续播放"。
            # 这是最可靠的恢复信号 —— 比靠"持续收到 PCM"推断更早、更准。
            log.info("AirPlay 事件: prsm（播放流已恢复）")
            self._handle_stream_resumed("prsm")
        elif code in (".", "pffr", "phb0", "phbt", "psnc"):
            # 关键时序事件：正常不刷屏，但定位 seek/恢复问题必看
            log.info("AirPlay 事件: %s %s", code, (item.text or "")[:60])
        elif code == "PICT":
            with self._artwork_lock:
                self.state.album_art = bytes(item.data)
                self.state.album_art_mime = _sniff_image_mime(item.data)
            log.info("收到封面图 %d 字节", len(item.data))
        else:
            # 未识别的事件也要留痕：真机上「拖动进度条无反应」很可能就是某个
            # 我们没处理的事件（或干脆什么事件都没发，靠音频兜底恢复）
            log.debug("AirPlay 未识别事件: type=%s code=%s payload=%s",
                      item.type, code, (item.text or "")[:60])

    def _capture_dacp(self, code: str, text: str) -> None:
        """捕获 DACP 反向控制凭据（``daid`` / ``acre`` / ``dapo``）。

        shairport-sync 会在元数据管道上发出这些字段；真实 AirPlay 2 发送端
        通常不发，因此这里只是「有就记下来」，没有也绝不猜测。
        """
        field_name = {"daid": "dacp_id", "acre": "active_remote", "dapo": "remote_port"}[code]
        value: Any = text.strip()
        if code == "dapo":
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = 0
        with self._lock:
            self.state.airplay_session[field_name] = value
        log.info("AirPlay 反向控制字段到达: %s=%s", code, value)
        self.update_remote_credentials()

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
            self.update_remote_credentials()
            log.info("AirPlay session established (client ip=%s)", payload)

    # ------------------------------------------------------------- 状态迁移
    def _handle_play(self, is_resume: bool) -> None:
        self.output.refresh_base_url()
        confirmed = self._confirm_pending("play")
        if confirmed is not None:
            log.info("DLNA 发起的 Play 已由 AirPlay 确认（request_id=%s）",
                     confirmed.get("request_id"))
        if is_resume and self._silence_active:
            # keepalive(方案A) 的恢复：渲染器一直在 PLAYING、URI 从未改变，
            # 因此**不做任何 UPnP 操作**，只把输出层切回真实 PCM 即可。
            self._resume_timeline = _ResumeTimeline(self.ring.byte_rate, "keepalive")
            self._resume_timeline.mark("T0_airplay_resume")
            self._stop_pause_keepalive("恢复播放")
            self.timeline.on_resume()
            self._enter(MACHINE_PLAYING, reason="keepalive-resume")
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
                need_rebuild = self.output.paused_with_stop
                token = self.output.gen_token
            if need_rebuild:
                log.info("恢复播放：渲染器此前无法暂停（Profile 声明），重建 DLNA 会话")
                self._begin_new_generation(reason="resume-rebuild", offset_ms=None)
                self._start_dlna_session(reason="resume-rebuild")
            elif token and self._start_pause_recovery():
                # 方案 A 已启动：等待渲染器自己重连（Range 0 会被映射到暂停位置）
                if self._resume_timeline is not None:
                    self._resume_timeline.mark("T2_fallback_decided")
                return
            elif token and self._pause_recovery_pending:
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
                self.output.set_intent(MODE_PLAY, token, set_uri=False)
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
            self.output.set_intent(MODE_PLAY, self.output.gen_token, set_uri=False)
            return

        # 真正的新播放位置（首次播放 / 换曲 / seek 到别处）才更换媒体生命周期。
        self._last_rebuild_at = 0.0
        self.output.reset_uri_count()
        self._begin_new_generation(reason="pbeg", offset_ms=None)
        self._start_dlna_session(reason="pbeg")

    def _can_resume_in_place(self) -> bool:
        """能否原地续播（只发 Play、不换代、不重设 URI）。

        必须同时满足：

        1. **渲染器确实处于 PAUSED** —— 真机上固件把 ``Pause`` 做成 ``Stop``，
           此时只发 ``Play`` 会"假播放"（报告 PLAYING、继续拉流、但无声）。
        2. **曲目位置连续** —— 位置跳变意味着用户 seek 到了别处，必须换代重建。
        """
        if self.state.renderer_state != RENDERER_PAUSED:
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
        token = self.output.gen_token
        session = self.streams.get(token) if token else None
        if session is None or session.closed:
            log.info("keepalive: 当前没有可用的流会话，退回 current 方案")
            return False
        self._silence_active = True
        self._silence_started_at = time.monotonic()
        self._keepalive_idle_ticks = 0
        self._silence_generation = self.ring.generation
        self.output.set_silence_mode(session, True)
        self._enter(MACHINE_PAUSED, reason="keepalive")
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
        token = self.output.gen_token
        session = self.streams.get(token) if token else None
        self.output.set_silence_mode(session, False)
        elapsed = time.monotonic() - self._silence_started_at
        self._silence_active = False
        log.info("keepalive(方案A): 结束静音（%s），输出层切回真实 PCM（保持 %.1fs，"
                 "期间未对渲染器做任何 UPnP 操作）", reason, elapsed)

    def _keepalive_udn(self) -> str:
        record = self.registry.selected()
        return record.udn if record is not None else ""

    def _keepalive_allowed(self) -> bool:
        """该渲染器是否还有资格尝试 keepalive（Renderer Profile）。"""
        # Profile 层面的一票否决。keepalive 的前提是「渲染器能停住且不丢弃会话」，
        # 而它的策略是**不对渲染器发任何 UPnP 命令**、只在输出层改送静音。
        # 真机证据（小爱音箱 S12）：它收到 Pause 会自行进入 STOPPED 并丢弃 HTTP 拉流
        # （真机日志 renderer=STOPPED 3724 次 vs PAUSED_PLAYBACK 2 次）。后果是：
        #   * 暂停：音箱先把已缓冲的真实 PCM 放完（约 5~6s）才轮到我们送的静音；
        #   * 恢复：音箱又要把缓冲里的静音放完（约 5~6s）才听到真实声音。
        # 这正是用户反馈的「暂停/恢复各延迟 5~6 秒」。对这种设备 keepalive 从原理上
        # 不成立，必须走 current（真实 Pause/Stop + 恢复时重新宣告）。
        try:
            profile = self.output.profile()
        except Exception:  # noqa: BLE001 - Profile 取不到时不得影响暂停流程
            profile = None
        if profile is not None and not profile.supports_pause:
            if not self._keepalive_profile_warned:
                self._keepalive_profile_warned = True
                log.info(
                    "keepalive 不适用：Renderer Profile(%s) 声明 supports_pause=False"
                    "（该设备的暂停实为 Stop 且丢弃 HTTP），暂停/恢复改用 current 方案，"
                    "避免 5~6 秒缓冲延迟", profile.name)
            return False
        return self._keepalive_capable.get(self._keepalive_udn(), True) is not False

    def _mark_keepalive_unsupported(self, reason: str) -> None:
        udn = self._keepalive_udn()
        if self._keepalive_capable.get(udn) is False:
            return
        self._keepalive_capable[udn] = False
        log.warning("keepalive: 标记渲染器 %s 不支持 keepalive（%s）——"
                    "后续暂停将直接使用 current 方案", udn or "?", reason)

    def _handle_stream_resumed(self, source: str) -> None:
        """AirPlay 明确表示播放流已恢复（``prsm``）：立即结束静音/暂停并恢复输出。"""
        if self._silence_active:
            # keepalive：渲染器一直在播，只需把输出层切回真实 PCM
            self._resume_timeline = _ResumeTimeline(self.ring.byte_rate, "keepalive")
            self._resume_timeline.mark("T0_airplay_resume")
            self._stop_pause_keepalive(source)
            self.timeline.on_resume()
            self._enter(MACHINE_PLAYING, reason=source)
            with self._lock:
                self._paused_audio_bytes = 0
            log.info("%s: 结束 keepalive 静音并切回真实 PCM（未对渲染器做任何 UPnP 操作）", source)
            return
        with self._lock:
            paused = self.state.state == PAUSED
            pending_audio = self._paused_audio_bytes
        if not paused:
            return
        threshold = int(self.ring.byte_rate * self.RESUMED_AUDIO_THRESHOLD_SECONDS)
        if pending_audio < threshold:
            # 位置信号到了但数据还没来 —— 交给轮询的音频兜底，
            # 避免在元数据线程里做网络操作
            self.timeline.on_resume()
            self._enter(MACHINE_PLAYING, reason=source)
            log.info("%s: 标记播放已恢复（等待音频数据到达）", source)
            return
        with self._lock:
            self._paused_audio_bytes = 0
        log.info("%s: 暂停状态下收到恢复信号且已有音频数据 → 主动重建 DLNA 会话", source)
        self._clear_pause_recovery(source)
        self._begin_new_generation(reason="stream-resumed", offset_ms=None)
        self._start_dlna_session(reason="stream-resumed")

    def _check_resumed_without_event(self) -> None:
        """兜底：暂停中却持续收到 PCM → 主动恢复播放。"""
        with self._lock:
            if self.state.state != PAUSED or self._silence_active:
                return
            pending = self._paused_audio_bytes
        threshold = int(self.ring.byte_rate * self.RESUMED_AUDIO_THRESHOLD_SECONDS)
        if pending < threshold:
            return
        log.info("检测到暂停期间持续收到音频（%.1fs），但没有任何 AirPlay 恢复事件 —— "
                 "判定播放已恢复，主动重建 DLNA 会话（新 generation 的 0 点 = 当前位置）",
                 pending / float(self.ring.byte_rate or 1))
        with self._lock:
            self._paused_audio_bytes = 0
        self._stop_pause_keepalive("音频已恢复")
        self._clear_pause_recovery("音频已恢复")
        self._begin_new_generation(reason="audio-resumed", offset_ms=None)
        self._start_dlna_session(reason="audio-resumed")

    def _check_keepalive_health(self) -> None:
        """keepalive 期间的断流检测（GPT 需求 Phase 2）。"""
        if not self._silence_active:
            return
        token = self.output.gen_token
        renderer_state = self.state.renderer_state
        session = self.streams.get(token) if token else None
        if session is None or session.closed:
            self._mark_keepalive_unsupported("流会话已失效")
            self._stop_pause_keepalive("流会话失效")
            return
        if renderer_state == RENDERER_STOPPED:
            self._mark_keepalive_unsupported("渲染器在暂停期间转入 STOPPED")
            self._stop_pause_keepalive("渲染器已停止")
            self.output.set_intent(MODE_PAUSE, token)
            return
        if session.clients <= 0:
            self._keepalive_idle_ticks += 1
            if self._keepalive_idle_ticks >= 3:      # 约 3 个轮询周期（≈9s）
                self._mark_keepalive_unsupported("渲染器停止拉流")
                self._stop_pause_keepalive("渲染器停止拉流")
                self.output.set_intent(MODE_PAUSE, token)
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
        self.output.set_intent(MODE_PAUSE, self.output.gen_token)

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
        """暂停位置的数据是否还在环形缓冲里（已被覆盖就只能 fallback 换代）。"""
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
        """尝试「方案 A」：保持 generation / URI 不变，让渲染器自己重连 HTTP。"""
        with self._lock:
            pending = self._pause_recovery_pending
            offset = self._paused_ring_offset
            position = self._paused_airplay_position_ms
            generation = self._paused_generation
        token = self.output.gen_token
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
        self.output.set_intent(MODE_PLAY, token, set_uri=False)
        return True

    def _check_pause_recovery(self) -> None:
        """方案 A 收尾：成功判定 / 超时 fallback。"""
        with self._lock:
            deadline = self._pause_recovery_deadline
            if deadline <= 0.0:
                return
        token = self.output.gen_token
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
            self._enter(RECOVERING, reason="pause-recovery-unavailable")
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
        self._enter(RECOVERING, reason="pause-recovery-fallback")
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
        """``pbeg`` 时判断能否沿用当前 DLNA 会话。"""
        with self._lock:
            token = self.output.gen_token
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
        confirmed = self._confirm_pending("pause")
        if confirmed is not None and confirmed.get("source") == SOURCE_DLNA:
            # 控制回环防护（第 19 节）：这个暂停是由 DLNA 端发起的，
            # AirPlay 只是回显了结果 —— **不得**再向渲染器重复下发 Pause。
            position = self.timeline.position_ms()
            self.timeline.on_pause()
            with self._lock:
                self._position_at_stream_boundary = position
            self._enter(MACHINE_PAUSED, reason="dlna-request-confirmed")
            log.info("DLNA 发起的 Pause 已由 AirPlay 确认（request_id=%s）："
                     "更新状态为 PAUSED，但不重复下发 DLNA Pause（防回环）",
                     confirmed.get("request_id"))
            return
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
        self._enter(MACHINE_PAUSED, reason="airplay-pause")
        self.output.set_intent(MODE_PAUSE, self.output.gen_token)
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
        confirmed = self._confirm_pending("seek")
        track_change = self._confirm_pending("next") or self._confirm_pending("previous")
        if track_change is not None:
            log.info("DLNA 发起的曲目切换已由 AirPlay 确认（%s, request_id=%s）",
                     track_change.get("action"), track_change.get("request_id"))
        self._cancel_transition("seek/flush")
        self._stop_pause_keepalive("seek")
        self._clear_pause_recovery("seek")
        self._handled_seek_at = time.monotonic()
        log.info("检测到 seek/flush (frame=%s)：换代并重锚 DLNA 会话", payload or "?")
        self._enter(TRACK_SWITCHING if track_change else SEEKING, reason="flush")
        if confirmed is not None:
            log.info("DLNA 发起的 Seek 已由 AirPlay 确认（request_id=%s）；"
                     "按新的 AirPlay 位置重建输出，不重复下发反向 Seek",
                     confirmed.get("request_id"))
        self._begin_new_generation(reason="flush", offset_ms=None)
        with self._lock:
            self._pending_reanchor = True
        # AirPlay 侧的新位置通过随后的 prgr 得知，preroll 期间即可修正
        self._start_dlna_session(reason="seek")

    def _handle_play_stream_end(self, reason: str) -> None:
        """``pend``：AirPlay 播放流结束 —— **不等价于**整个会话结束。"""
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
        self._enter(STOPPING, reason=reason)
        self.timeline.on_stop()
        self.ring.flush()
        with self._lock:
            self.state.state = STOPPED
            self.state.machine_state = IDLE
            self.machine_state = IDLE
            self.state.title = ""
            self.state.artist = ""
            self.state.album = ""
            self.state.album_art = None
            self.state.duration_ms = None
            self.state.position_ms = None
            self.state.audio_format = ""
            self.state.airplay_session = {}
            self.state.renderer_state = RENDERER_STOPPED
            self._pending_action = ""
            self._pending_source = ""
            self._pending_request_id = ""
            self.state.pending_request = ""
            self.state.control_source = ""
            self.state.request_id = ""
            self._awaiting_new_stream = False
            self._transition_deadline = 0.0
        self.output.clear_session_tokens()
        self._stop_pause_keepalive("播放结束")
        self._clear_pause_recovery("播放结束")
        self._session_started_at = 0.0
        self._rel_sample = None
        self._rel_offset_ms = None
        self._rel_rate = None
        self._last_rebuild_at = 0.0
        self.streams.close_all()
        self.output.set_intent(MODE_STOP, "")
        self.remote.clear()

    def _handle_volume(self, item: MetadataItem) -> None:
        values = item.csv_numbers()
        if not values:
            log.debug("无法解析 pvol 载荷: %r", item.text)
            return
        airplay_db = values[0]
        percent, muted = stream_airplay_db_to_percent(airplay_db)
        if not self.output.accept_airplay_volume():
            return
        with self._lock:
            self.state.volume = percent
            self.state.muted = muted
        log.info("SetVolume: AirPlay %.2f dB -> %d%%%s", airplay_db, percent,
                 " (mute)" if muted else "")
        self.output.note_volume_from_airplay(percent)

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
        # 恢复预缓冲：**不再向 AirPlay 环形缓冲写入静音**（ARCHITECTURE_V2 第 23/28 节
        # 明确禁止）。这段空窗改由 DLNA Output 层用静音填充（continuous_output），
        # AirPlay RingBuffer / Timeline 永远只有真实音频。
        prebuffer_ms = int(self.config.get("resume_prebuffer_ms") or 0)
        if prebuffer_ms > 0:
            log.info("恢复预缓冲 %d ms：由 DLNA Output 层以静音填充"
                     "（不写入 AirPlay RingBuffer，保持时间线真实）", prebuffer_ms)
        with self._lock:
            self.state.duration_ms = None
            self.state.position_ms = None
        if self.machine_state not in (SEEKING, TRACK_SWITCHING, RECOVERING):
            self._enter(MACHINE_BUFFERING, reason=reason)
        self.timeline.begin_generation(generation, offset_ms)
        if self._resume_timeline is not None:
            self._resume_timeline.mark("T4_generation_done")
        log.info("音频缓冲换代: gen=%d 原因=%s", generation, reason)

    def _start_dlna_session(self, reason: str, play: bool = True) -> None:
        """创建新一代 Virtual Media Resource 并请求 DLNA Output 去播放它。"""
        record = self.registry.selected()
        if record is None:
            log.warning("尚未选择 DLNA Renderer，无法开始播放（%s）", reason)
            self._enter(IDLE, legacy=ERROR, reason="no-renderer")
            return
        # resolve_stream_kind 返回 (content_type, kind)，注意顺序
        _content_type, kind = self.registry.resolve_stream_kind(
            record, self.config.get("output_format")
        )
        duration = self.timeline.duration_ms()
        session = self.output.new_media_session(reason, kind, duration)
        session.on_bytes = self._note_resume_bytes
        session.on_connect = self._note_resume_connect
        self.output.set_intent(MODE_PLAY, session.token, play=play)
        if self.machine_state not in (SEEKING, TRACK_SWITCHING, RECOVERING):
            self._enter(MACHINE_BUFFERING, reason=reason)

    # ------------------------------------------------------------ 位置轮询
    def _poll_loop(self) -> None:
        self._stop_event.wait(2.0)
        while not self._stop_event.is_set():
            interval = float(self.config.get("metadata_poll_seconds"))
            try:
                self._poll_once()
                self.output.renew_subscriptions_if_due()
                # pend 过渡窗口到期检查（不依赖渲染器是否可达）
                self._check_transition_timeout()
                # 暂停恢复（方案 A）的成功判定 / 超时 fallback
                self._check_pause_recovery()
                # 兜底：暂停中却持续收到音频 → AirPlay 已恢复（无事件也生效）
                self._check_resumed_without_event()
                # keepalive（方案 A'）断流检测与超时退化（GPT Phase 2）
                self._check_keepalive_health()
                self._check_keepalive_timeout()
                # 连续输出层报告真实 PCM 长时间中断 → RECOVERING
                self._check_silence_timeout()
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

    def _check_silence_timeout(self) -> None:
        """连续输出层报告真实 PCM 长时间中断 → RECOVERING。

        静音本身保证 HTTP 不断（第 6 节）；这里只在链路确实不可用时执行
        「new generation + SetAVTransportURI + Play」原语（第 8 / 26 节）。
        """
        if not self.output.take_silence_timeout():
            return
        with self._lock:
            state_name = self.state.state
            renderer_state = self.state.renderer_state
        if state_name not in (PLAYING, BUFFERING):
            return
        self._enter(RECOVERING, reason="silence-timeout")
        session = (self.streams.get(self.output.renderer_token)
                   or self.streams.get(self.output.gen_token))
        clients = getattr(session, "clients", 0) if session is not None else 0
        log.warning("连续输出静音超时：进入 RECOVERING（渲染器=%s 拉流连接=%d）",
                    renderer_state, clients)
        if clients > 0 and renderer_state == RENDERER_PLAYING:
            log.info("静音超时但渲染器仍在拉流：继续用静音保持输出，等待 AirPlay 数据恢复")
            return
        now = time.monotonic()
        if now - self._last_stall_rebuild <= self.REBUILD_COOLDOWN_SECONDS:
            return
        self._last_stall_rebuild = now
        self._begin_new_generation(reason="silence-timeout", offset_ms=None)
        self._start_dlna_session(reason="silence-timeout")

    def _poll_once(self) -> None:
        record = self.registry.selected()
        if record is None or record.client is None:
            return
        client = record.client
        try:
            info = client.get_transport_info()
        except Exception as exc:  # noqa: BLE001
            self.output.note_action_failure(record, "GetTransportInfo", str(exc))
            return

        renderer_state = (info.get("state") or "").upper() or RENDERER_STOPPED
        with self._lock:
            self.state.renderer_state = renderer_state
            if renderer_state == RENDERER_PLAYING and self.state.state in (BUFFERING, PAUSED):
                self.state.state = PLAYING
                self.state.machine_state = MACHINE_PLAYING
                self.machine_state = MACHINE_PLAYING
            elif renderer_state == RENDERER_PAUSED and self.state.state == PLAYING:
                self.state.state = PAUSED
                self.state.machine_state = MACHINE_PAUSED
                self.machine_state = MACHINE_PAUSED
        self.output.reset_action_errors()

        # 渲染器把 Pause 实现成了 Stop（真机实测）：标记「待暂停恢复」，
        # Resume 时优先走方案 A（等它自己重连 HTTP，把逻辑 0 点映射到暂停位置）。
        if (renderer_state == RENDERER_STOPPED
                and self.state.state == PAUSED
                and self._paused_ring_offset is not None
                and not self._pause_recovery_pending
                and self._paused_generation == self.ring.generation):
            with self._lock:
                self._pause_recovery_pending = True
            log.info("暂停恢复: 检测到渲染器实际为 STOPPED（固件把 Pause 实现成 Stop）"
                     "，已标记待恢复；Resume 时将优先尝试「重连 Range 0 → 映射到暂停位置」")

        if renderer_state in (RENDERER_PLAYING, RENDERER_PAUSED):
            try:
                position = client.get_position_info()
            except Exception as exc:  # noqa: BLE001
                log.debug("GetPositionInfo 失败: %s", exc)
                position = None
            if position:
                rel = position.get("rel_time_ms")
                if rel is not None and float(rel) > 0:
                    self._maybe_report_resume(
                        self.streams.get(self.output.renderer_token), float(rel))
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
        """兜底：渲染器声称在播放，却长时间没有拉取音频流。"""
        if self._pause_recovery_pending:
            # 方案 A 进行中：渲染器可能刚 STOPPED、正准备重连，绝不能被兜底重建打断
            return
        if self.state.state != PLAYING or not self.output.renderer_token:
            return
        session = self.streams.get(self.output.renderer_token)
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
        self._enter(RECOVERING, reason="stalled")
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

    def _mark_resume(self, name: str) -> None:
        """DLNA Output 报告恢复耗时里程碑。"""
        timeline = self._resume_timeline
        if timeline is not None and not timeline.reported:
            timeline.mark(name)

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
        """把 DLNA 侧的位置信息当作**观测值**：只诊断，绝不驱动重建。"""
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
        token = self.output.renderer_token
        session = self.streams.get(token) if token else None
        with self._lock:
            state_name = self.state.state
            renderer_state = self.state.renderer_state
            machine = self.machine_state
        log.info(
            "诊断: airplay_pos=%s state=%s machine=%s renderer=%s rel_time=%s offset=%s rate=%s "
            "gen=%s token=%s http_clients=%s bytes_served=%s last_range=%s "
            "idle=%.1fs uri_count=%d last_rebuild=%s awaiting_new_stream=%s pause_recovery=%s "
            "keepalive_capable=%s tl_playing=%s pending=%s",
            int(self.timeline.position_ms()) if self.timeline.position_ms() is not None else "-",
            state_name, machine, renderer_state,
            int(self.timeline.renderer_rel_time_ms) if self.timeline.renderer_rel_time_ms is not None else "-",
            int(self._rel_offset_ms) if self._rel_offset_ms is not None else "-",
            "%.4f" % self._rel_rate if self._rel_rate is not None else "-",
            self.ring.generation, token[-6:] if token else "-",
            session.clients if session is not None else 0,
            int(session.bytes_served) if session is not None else 0,
            self._last_range_info or "-",
            (now - session.last_activity) if session is not None and session.last_activity else -1.0,
            self.output.uri_count, self._last_rebuild_reason or "-", self._awaiting_new_stream,
            ("active" if self._pause_recovery_deadline > 0.0
             else ("armed" if self._pause_recovery_pending else "-")),
            self._keepalive_capable.get(self._keepalive_udn(), None),
            getattr(self.timeline, "_playing", None),
            _ACTION_TO_REQUEST.get(self._pending_action, "-"),
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

    # ------------------------------------------------------------- GENA 通知
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
                if volume is not None:
                    accepted = self.output.accept_renderer_volume(volume)
                    if accepted is not None:
                        self.state.volume = accepted
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
    def artwork(self) -> tuple[Optional[bytes], str]:
        with self._artwork_lock:
            return self.state.album_art, self.state.album_art_mime

    def reset_for_renderer_change(self) -> None:
        """用户切换渲染器：结束旧会话（不伪造任何状态）。"""
        self.output.set_intent(MODE_STOP, "")
        with self._lock:
            self.state.renderer_state = RENDERER_STOPPED
        self.output.clear_session_tokens()

    def diagnostics(self) -> dict[str, Any]:
        """Virtual Player 侧的诊断信息（配合 DLNAOutput.diagnostics）。"""
        with self._lock:
            return {
                "machine_state": self.machine_state,
                "pending_request": _ACTION_TO_REQUEST.get(self._pending_action, ""),
                "pending_action": self._pending_action,
                "control_source": self._pending_source,
                "request_id": self._pending_request_id,
                "renderer_rel_time_ms": self.timeline.renderer_rel_time_ms,
                "rel_offset_ms": self._rel_offset_ms,
                "rel_rate": self._rel_rate,
                "last_rebuild_reason": self._last_rebuild_reason,
                "awaiting_new_stream": self._awaiting_new_stream,
                "transition_reason": self._transition_reason,
                "pause_recovery_pending": self._pause_recovery_pending,
                "pause_recovery_deadline": self._pause_recovery_deadline,
                "keepalive_active": self._silence_active,
                "keepalive_capable": dict(self._keepalive_capable),
                "silence_generation": self._silence_generation,
                "renderer_reconnect_capable": self._renderer_reconnect_capable,
                "session_started_at": self._session_started_at,
                "last_stall_rebuild": self._last_stall_rebuild,
                "paused_airplay_position_ms": self._paused_airplay_position_ms,
                "paused_ring_offset": self._paused_ring_offset,
                "paused_generation": self._paused_generation,
                "reverse_control_attempts": self._reverse_control_attempts,
                "reverse_control_success": self._reverse_control_success,
                "last_reverse_result": dict(self._last_reverse_result),
            }

    def reverse_control_status(self) -> dict[str, Any]:
        return {
            "capabilities": {name: self.remote.capability(name) for name in CAPABILITIES},
            "credentials": self.remote.credentials.to_dict(),
            "attempts": self._reverse_control_attempts,
            "success": self._reverse_control_success,
            "last_result": dict(self._last_reverse_result),
            "note": ("架构已实现 DACP 反向控制；AirPlay 2 + iPhone 环境实测不提供 "
                     "daid/acre/dapo，因此能力通常为 UNKNOWN，Virtual Player 不会伪造状态。"),
        }


# --------------------------------------------------------------------- 小工具
def stream_airplay_db_to_percent(db: float) -> tuple[int, bool]:
    """延迟导入 ``stream.airplay_db_to_percent``（避免模块级循环依赖）。"""
    from .stream import airplay_db_to_percent

    return airplay_db_to_percent(db)


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
