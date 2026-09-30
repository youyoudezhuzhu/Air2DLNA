"""DLNA Output：AVTransport 控制 + 连续 HTTP 媒体输出（含静音生成）。

对应 ARCHITECTURE_V2 第 4、5、6、7、8、10、11、21、24、26 节。

职责边界
--------

Virtual Player **不**直接操作 ``SetAVTransportURI`` / ``Play`` / ``Pause`` / HTTP
连接；这些全部由本模块负责：

* 把 Virtual Player 的意图（PLAY / PAUSE / STOP / 换媒体会话）翻译成
  UPnP AVTransport 动作；
* 管理「当前媒体资源」（流会话 / token / URI / DIDL 元数据）；
* 生成 Virtual Media Resource 的连续输出：真实 PCM 不足时用静音填充，
  **绝不主动 EOF**（静音只在输出层生成，绝不写入 AirPlay RingBuffer/Timeline）；
* 通过 :class:`air2dlna.renderer_profile.RendererProfile` 适配具体设备的差异
  （例如小爱音箱 S12 的 Pause→STOPPED），Virtual Player 核心保持设备无关。

Recovery（新一代 + SetAVTransportURI + Play）只是本模块提供的一个**原语**，
由 Virtual Player 在 RECOVERING 状态下显式调用，绝不是正常播放路径。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import netif, stream, upnp
from .renderer_profile import GENERIC_PROFILE, RendererProfile
from .ringbuffer import PcmRingBuffer, StaleGeneration
from .timeline import AudioTimeline

log = logging.getLogger("dlna_output")

# 收敛目标
MODE_STOP = "stop"
MODE_PLAY = "play"
MODE_PAUSE = "pause"

# 渲染器上报的 UPnP 传输状态
RENDERER_PLAYING = "PLAYING"
RENDERER_PAUSED = "PAUSED_PLAYBACK"
RENDERER_STOPPED = "STOPPED"
RENDERER_TRANSITIONING = "TRANSITIONING"

# 播放状态（对外暴露的 legacy 值，与 Virtual Player 的机器状态映射）
STOPPED = "STOPPED"
PAUSED = "PAUSED"
PLAYING = "PLAYING"
BUFFERING = "BUFFERING"
ERROR = "ERROR"

# 连续 SOAP 失败多少次判定渲染器离线
_MAX_ACTION_ERRORS = 5


@dataclass
class _Intent:
    """DLNA 收敛线程要达成的目标。"""

    mode: str = MODE_STOP
    token: str = ""          # 要播放的流 token
    volume: Optional[int] = None
    revision: int = 0
    #: 是否在 SetAVTransportURI 之后立即发送 Play（prewarm 预热时置 False）
    play: bool = True
    #: 是否允许由该意图触发 ``SetAVTransportURI``。
    #: 复用同一个 generation 时必须为 False —— DLNA 语义下重设 URI 会让渲染器
    #: 从该资源的**开头**重新播放（真机现象：「暂停后恢复变成从头播放」）。
    set_uri: bool = True


class DLNAOutput:
    """把 Virtual Player 的意图变成具体的 UPnP / HTTP 输出行为。"""

    def __init__(self, config, registry, ring: PcmRingBuffer, timeline: AudioTimeline,
                 streams: stream.StreamManager, state, lock: threading.RLock,
                 stop_event: threading.Event, log: Optional[Callable[..., None]] = None,
                 profile_provider: Optional[Callable[[Any], RendererProfile]] = None) -> None:
        self.config = config
        self.registry = registry
        self.ring = ring
        self.timeline = timeline
        self.streams = streams
        #: 与 Virtual Player **共享**的对外状态对象（渲染器状态、音量、播放状态）
        self.state = state
        self._lock = lock
        self._stop_event = stop_event
        self.log = log or (lambda msg, *a: None)
        self._profile_provider = profile_provider or (lambda _record=None: GENERIC_PROFILE)

        self._intent = _Intent()
        self._intent_event = threading.Event()
        self._intent_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._converge_dirty = False

        #: 当前已创建的媒体资源 token（Virtual Media Session）
        self._gen_token = ""
        #: 渲染器**实际**已经被我们设置为播放的流 token
        self._renderer_token = ""
        self._paused_with_stop = False
        self._action_errors = 0
        self._uri_reason = ""
        #: 每次 SetAVTransportURI 的序号与原因（验收要求：正常播放应该只有 1 次）
        self._uri_count = 0
        self._uri_log: list[str] = []
        self._subscriptions: dict[str, str] = {}
        self._last_renew_monotonic = 0.0
        self._last_volume_tx = 0.0
        self._last_volume_rx = 0.0
        #: 当前 DLNA 会话的建立时刻
        self._session_started_at = 0.0
        #: 连续输出层报告「真实 PCM 长时间中断」（由 HTTP 输出线程置位）
        self._silence_timeout_pending = False
        self._http_port = int(config.get("http_port"))
        self._base_url = ""
        #: 回调：通知 Virtual Player 发生状态变化 / 记录恢复耗时标记
        self.on_state_change: Optional[Callable[[str], None]] = None
        self.on_mark: Optional[Callable[[str], None]] = None
        self.refresh_base_url()

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop_event.clear()
        self._worker = threading.Thread(target=self.worker_loop, name="dlna-worker", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._intent_event.set()
        thread = self._worker
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    def refresh_base_url(self) -> None:
        """重新探测对外 IP（Virtual Media Resource 的 host 部分）。"""
        address = netif.primary_lan_address()
        self._base_url = f"http://{address}:{self._http_port}"

    # ------------------------------------------------------------------ 查询
    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def gen_token(self) -> str:
        return self._gen_token

    @property
    def renderer_token(self) -> str:
        return self._renderer_token

    @property
    def uri_count(self) -> int:
        return self._uri_count

    @property
    def intent_mode(self) -> str:
        return self._intent.mode

    @property
    def intent_token(self) -> str:
        return self._intent.token

    @property
    def paused_with_stop(self) -> bool:
        """渲染器是否「被暂停成了 Stop」（恢复时必须重新宣告媒体会话）。"""
        return self._paused_with_stop

    def reset_uri_count(self) -> None:
        """一次正常播放只应有一次 SetAVTransportURI（验收要求）。"""
        self._uri_count = 0

    def note_action_failure(self, record, action: str, detail: str) -> None:
        """把一次渲染器读取/控制失败记入错误计数（供 Virtual Player 轮询使用）。"""
        self._note_action_failure(record, action, detail)

    def reset_action_errors(self) -> None:
        self._action_errors = 0

    def profile(self, record=None) -> RendererProfile:
        """当前渲染器的 Profile（由 BridgeController 注入的 provider 决定）。"""
        record = record if record is not None else self.registry.selected()
        try:
            return self._profile_provider(record)
        except Exception:  # noqa: BLE001 - Profile 选择失败必须降级到 generic
            log.debug("Renderer Profile 选择失败，使用 generic", exc_info=True)
            return GENERIC_PROFILE

    def buffer_plan(self, record=None) -> dict[str, Any]:
        """输出缓冲水位（ARCHITECTURE_V2 第 9 节）。

        默认 target ≈ 1000ms；``minimum_buffer_ms`` / ``target_buffer_ms`` /
        ``maximum_buffer_ms`` 可被配置覆盖，**不强制精确 1 秒**。
        实际预滚动 = max(preroll_seconds, target_buffer_ms/1000)，用于吸收
        AirPlay → Virtual Player → DLNA 之间的短暂抖动。
        """
        profile = self.profile(record)
        try:
            preroll = float(self.config.get("preroll_seconds") or 0.0)
        except (TypeError, ValueError):
            preroll = 0.0
        # 配置里显式给出缓冲水位时使用之（生产配置始终有）；否则沿用旧的
        # preroll_seconds 行为（测试替身/精简配置不会因此多等 1 秒）。
        raw_target = self.config.get("target_buffer_ms")
        if raw_target is None:
            minimum_ms = 0
            target_ms = int(max(0.0, preroll) * 1000.0)
            maximum_ms = profile.maximum_buffer_ms
        else:
            try:
                minimum_ms = int(self.config.get("minimum_buffer_ms")
                                 or profile.minimum_buffer_ms)
                target_ms = int(raw_target) or profile.target_buffer_ms
                maximum_ms = int(self.config.get("maximum_buffer_ms")
                                 or profile.maximum_buffer_ms)
            except (TypeError, ValueError):
                minimum_ms = profile.minimum_buffer_ms
                target_ms = profile.target_buffer_ms
                maximum_ms = profile.maximum_buffer_ms
        target_ms = max(0, target_ms, minimum_ms)
        return {
            "minimum_buffer_ms": max(0, minimum_ms),
            "target_buffer_ms": target_ms,
            "maximum_buffer_ms": max(target_ms, maximum_ms),
            "target_seconds": max(preroll, target_ms / 1000.0),
            "preroll_seconds": preroll,
            "buffer_behavior": profile.buffer_behavior,
        }

    def session_for(self, token: str):
        return self.streams.get(token) if token else None

    # ------------------------------------------------------------- 媒体会话
    def new_media_session(self, reason: str, kind: str,
                          duration_ms: Optional[float] = None) -> Any:
        """创建一代新的 Virtual Media Resource（唯一 URI / token）。

        连续输出与静音在会话上打开：DLNA 播放期间真实 PCM 不足时由输出层
        生成静音，绝不 EOF（第 6 节）。
        """
        self._uri_reason = reason
        session = self.streams.new_generation(kind, duration_ms)
        continuous = self.config.get("continuous_output")
        session.continuous_output = True if continuous is None else bool(continuous)
        session.silence_timeout_s = self._silence_timeout_seconds()
        session.on_silence_timeout = self.note_silence_timeout
        session.silence_mode = False
        self._gen_token = session.token
        self._paused_with_stop = False
        return session

    def clear_session_tokens(self) -> None:
        self._gen_token = ""
        self._renderer_token = ""
        self._paused_with_stop = False

    def set_silence_mode(self, session, enabled: bool) -> None:
        """暂停 keepalive：输出层改送静音（不读环形缓冲、不污染 AirPlay 时间线）。"""
        if session is not None:
            session.silence_mode = bool(enabled)

    def set_continuous_output(self, session, enabled: bool) -> None:
        if session is not None:
            session.continuous_output = bool(enabled)

    def _silence_timeout_seconds(self) -> float:
        try:
            value = float(self.config.get("silence_timeout_seconds"))
        except (TypeError, ValueError):
            return 8.0
        return max(0.0, min(600.0, value))

    def note_silence_timeout(self) -> None:
        """HTTP 输出线程回调：真实 PCM 中断超过阈值（只置位，不做网络操作）。"""
        with self._lock:
            self._silence_timeout_pending = True

    def take_silence_timeout(self) -> bool:
        """取出并清除「静音超时」标志（由 Virtual Player 轮询调用）。"""
        with self._lock:
            pending = self._silence_timeout_pending
            self._silence_timeout_pending = False
            return pending

    # ------------------------------------------------------------- 意图下发
    def set_intent(self, mode: str, token: str, volume: Optional[int] = None,
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

    def mark_dirty(self) -> None:
        """上一轮收敛失败需要重试。"""
        self._converge_dirty = True

    # ------------------------------------------------------- 收敛线程
    def worker_loop(self) -> None:
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
                self.converge(intent)
            except Exception:  # noqa: BLE001 - 收敛线程必须长期存活
                log.exception("DLNA 收敛过程异常")
                self._converge_dirty = True
                time.sleep(1.0)

    def converge(self, intent: _Intent) -> None:
        record = self.registry.selected()
        client = record.client if record is not None else None
        if record is None or client is None:
            if intent.mode != MODE_STOP:
                log.warning("Renderer 未选中或不可用，无法执行 %s", intent.mode)
            return

        # 音量是独立维度，每次都尝试同步
        if intent.volume is not None:
            self.apply_volume(record, client, intent.volume)

        if intent.mode == MODE_STOP:
            self._safe_call(record, client.stop, "Stop")
            with self._lock:
                self.state.renderer_state = RENDERER_STOPPED
                self.state.state = STOPPED
                self._renderer_token = ""
            self._notify_state(STOPPED)
            return

        if intent.mode == MODE_PAUSE:
            self.do_pause(record, client)
            return

        if intent.mode == MODE_PLAY:
            self.do_play(record, client, intent.token, set_uri=intent.set_uri,
                         play=intent.play)

    # ------------------------------------------------------------- 播放动作
    def do_play(self, record, client, token: str, set_uri: bool = True,
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
        if already_this_token and current_renderer_state == RENDERER_PLAYING:
            with self._lock:
                self.state.state = PLAYING
            self._notify_state(PLAYING)
            return

        # 情况 2：同一条流被暂停 —— 直接 Play 续播
        if already_this_token and current_renderer_state == RENDERER_PAUSED:
            if self._safe_call(record, client.play, "Play"):
                with self._lock:
                    self.state.state = PLAYING
                    self.state.renderer_state = RENDERER_PLAYING
                    self._paused_with_stop = False
                self._notify_state(PLAYING)
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
                    self.state.renderer_state = RENDERER_PLAYING
                    self._paused_with_stop = False
                self._notify_state(PLAYING)
                return
            log.warning("渲染器拒绝 Play，回退为重建会话（SetAVTransportURI）")

        # 情况 3：全新开始 / seek 后重建
        plan = self.buffer_plan(record)
        needed = int(plan["target_seconds"] * self.ring.byte_rate)
        generation = session.generation
        try:
            ready = self.ring.wait_for_data(0, generation, needed, timeout=8.0)
        except StaleGeneration:
            # 预滚动等待期间缓冲又换代了（seek 会连续产生多代）。**绝不能让它抛出
            # converge**：那会中断整轮收敛，SetAVTransportURI/Play 都发不出去，
            # 渲染器拿不到新 URI —— 真机表现就是「拖进度条后完全没有声音」。
            # 正确做法是把意图重新指向最新一代，让收敛线程再跑一轮。
            newest = getattr(self.streams, "current", None)
            log.warning(
                "预滚动期间缓冲换代（gen=%s → %s）：改用最新一代重新收敛，"
                "避免中断 SetURI/Play",
                generation, getattr(newest, "generation", "?"))
            if newest is not None and newest.token != token:
                self.set_intent(MODE_PLAY, newest.token, set_uri=True, play=True)
            return
        if not ready:
            try:
                available = self.ring.write_offset
            except Exception:  # noqa: BLE001
                available = 0
            log.warning(
                "预滚动等待不足（需要 %.1fs，实际 %.1fs），仍尝试开始播放",
                plan["target_seconds"],
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
        self._mark("T5_seturi_sent")
        if not self._safe_call(record, client.set_av_transport_uri, "SetAVTransportURI",
                               uri=uri, metadata=metadata):
            return
        self._mark("T6_seturi_returned")
        if not play:
            # prewarm（方案 B）：只把新 URI 交给渲染器并让它自行建连/预缓冲，
            # 不发 Play —— 避免暂停期间漏出声音。恢复时只补一个 Play。
            log.info("prewarm(方案B): 已 SetAVTransportURI 但不 Play，等待用户恢复时再播放")
            with self._lock:
                self.state.state = PAUSED
            self._notify_state(PAUSED)
            return
        # 部分渲染器在 SetAVTransportURI 之后需要短暂准备
        time.sleep(0.3)
        self._mark("T7_play_sent")
        if not self._safe_call(record, client.play, "Play"):
            return
        with self._lock:
            self.state.state = PLAYING
            self.state.renderer_state = RENDERER_PLAYING
            self._paused_with_stop = False
            self._renderer_token = session.token
        self._notify_state(PLAYING)
        self.timeline.update_renderer_position(0.0)
        # 记录会话建立时刻：渲染器此时还在缓冲、位置会滞后，宽限期内不做漂移判定
        self._session_started_at = time.monotonic()
        self.ensure_subscriptions(record)

    def do_pause(self, record, client) -> None:
        """暂停渲染器。

        通用设备优先使用真正的 ``AVTransport#Pause``（保留 URI/位置）；
        如果该设备的 Profile 声明 ``supports_pause=False``（例如小爱音箱 S12，
        实测 Pause 后会自行进入 STOPPED），则由 Profile 决定直接使用 Stop ——
        这是**设备兼容行为**，不写在 Virtual Player 核心（第 11/12/21 节）。
        """
        if self.state.renderer_state == RENDERER_STOPPED:
            with self._lock:
                self.state.state = PAUSED
            self._notify_state(PAUSED)
            return
        profile = self.profile(record)
        if not profile.supports_pause:
            log.info(
                "Profile %s: supports_pause=False（该设备不会停留在 PAUSED，"
                "实测会自行转入 STOPPED）→ 直接使用 Stop，恢复时由 Virtual Player "
                "重新宣告媒体会话", profile.name)
            if self._safe_call(record, client.stop, "Stop"):
                with self._lock:
                    self.state.state = PAUSED
                    self.state.renderer_state = RENDERER_STOPPED
                    self._paused_with_stop = True
                    self._renderer_token = ""
                self._notify_state(PAUSED)
            return
        if self._safe_call(record, client.pause, "Pause"):
            with self._lock:
                self.state.state = PAUSED
                self.state.renderer_state = RENDERER_PAUSED
            self._notify_state(PAUSED)
            return
        log.warning(
            "渲染器不支持暂停实时流（Pause 失败），退化为 Stop；恢复时将重建会话"
        )
        if self._safe_call(record, client.stop, "Stop"):
            with self._lock:
                self.state.state = PAUSED
                self.state.renderer_state = RENDERER_STOPPED
                self._paused_with_stop = True
                self._renderer_token = ""
            self._notify_state(PAUSED)

    def apply_volume(self, record, client, percent: int) -> None:
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

    def accept_airplay_volume(self) -> bool:
        """抑制渲染器回声：刚由我们下发过的音量值不再回灌。"""
        with self._lock:
            if time.monotonic() - self._last_volume_rx < 1.0:
                return False
            self._last_volume_tx = time.monotonic()
            return True

    def note_volume_from_airplay(self, percent: int) -> None:
        """AirPlay 音量到达后，请求收敛线程同步给渲染器。"""
        self.set_intent(self.intent_mode, self.intent_token, volume=int(percent))

    def accept_renderer_volume(self, volume: int) -> Optional[int]:
        """渲染器上报的音量：若刚由我们下发过（回声）则忽略，否则接受。

        返回应当写入状态的音量；``None`` 表示忽略这次上报。
        """
        with self._lock:
            if time.monotonic() - self._last_volume_tx <= 1.0:
                return None
            self._last_volume_rx = time.monotonic()
            return max(0, min(100, int(volume)))

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
        log.error("DLNA SOAP 失败 (action=%s, udn=%s): %s", action,
                  getattr(record, "udn", "?"), detail)
        if self._action_errors >= _MAX_ACTION_ERRORS and record is not None:
            self.registry.mark_offline(record.udn, f"{action} 连续失败")
            with self._lock:
                self.state.state = ERROR
                self.state.renderer_state = "OFFLINE"
            self._notify_state(ERROR)

    def _mark(self, name: str) -> None:
        if self.on_mark is not None:
            try:
                self.on_mark(name)
            except Exception:  # noqa: BLE001 - 诊断标记不得影响输出
                pass

    def _notify_state(self, legacy_state: str) -> None:
        if self.on_state_change is not None:
            try:
                self.on_state_change(legacy_state)
            except Exception:  # noqa: BLE001
                log.debug("状态变化回调异常", exc_info=True)

    # ------------------------------------------------------------ GENA 订阅
    def ensure_subscriptions(self, record) -> None:
        """订阅 AVTransport 与 RenderingControl 事件（失败则完全依赖轮询）。"""
        if record is None or record.client is None:
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

    def renew_subscriptions_if_due(self) -> None:
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

    # ------------------------------------------------------------------ 诊断
    def diagnostics(self) -> dict[str, Any]:
        session = self.session_for(self._renderer_token)
        return {
            "gen_token": self._gen_token,
            "renderer_token": self._renderer_token,
            "uri_count": self._uri_count,
            "uri_log": list(self._uri_log),
            "uri_reason": self._uri_reason,
            "paused_with_stop": self._paused_with_stop,
            "session_started_at": self._session_started_at,
            "http_clients": session.clients if session is not None else 0,
            "bytes_served": int(session.bytes_served) if session is not None else 0,
            "silence_bytes": int(session.silence_bytes) if session is not None else 0,
            "continuous_output": bool(session.continuous_output) if session is not None else False,
            "buffer_plan": self.buffer_plan(),
            "renderer_profile": self.profile().name,
            "base_url": self._base_url,
            "silence_timeout_pending": self._silence_timeout_pending,
        }

    def status(self) -> dict[str, Any]:
        return {
            "intent": {"mode": self._intent.mode, "token": self._intent.token},
            "diagnostics": self.diagnostics(),
        }

    # ------------------------------------------- 历史方法名兼容（BridgeController 转发）
    # 重构前这些名字在 BridgeController 上；保留别名让既有调用方/测试无需修改。
    _do_play = do_play
    _do_pause = do_pause
    _worker_loop = worker_loop
    _converge = converge
    _apply_volume = apply_volume
    _ensure_subscriptions = ensure_subscriptions
    _renew_subscriptions_if_due = renew_subscriptions_if_due
    _refresh_base_url = refresh_base_url
