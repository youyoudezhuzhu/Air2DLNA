"""组合根：把 AirPlay 事件、Virtual Player、DLNA Output 粘合在一起。

架构（ARCHITECTURE_V2）：

    AirPlay（Playback Authority）
        → Virtual Player（State Coordinator）   virtual_player.VirtualPlayer
        → DLNA Output（Output Renderer）        dlna_output.DLNAOutput
        → 渲染器 / 连续 HTTP 媒体输出           stream.StreamManager
        ← AirPlay 反向控制（DACP）              airplay_remote.AirPlayRemoteController
        ← 设备差异                              renderer_profile.RendererProfile

本模块**不再承载状态机逻辑**：``BridgeController`` 只做三件事：

1. 组装上述模块（唯一状态对象、共享锁、停止事件、Renderer Profile 选择器）；
2. 提供生命周期（``start`` / ``stop``）与对外查询（``status`` / ``artwork`` /
   ``set_renderer``）；
3. 为历史调用方（``bridge.py`` / ``webui.py`` / 既有单元测试）保留既有属性与方法名，
   把它们转发给 Virtual Player / DLNA Output。

因此本节里的 ``_handle_play`` / ``_intent`` / ``_gen_token`` 等名字都只是**转发**，
真正的实现见 :mod:`air2dlna.virtual_player` 与 :mod:`air2dlna.dlna_output`。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from .airplay_remote import AirPlayRemoteController
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
    RENDERER_TRANSITIONING,
    STOPPED,
    DLNAOutput,
    _Intent,
)
from .renderer_profile import GENERIC_PROFILE, PROFILES, RendererProfile, select_profile
from .ringbuffer import PcmRingBuffer
from .timeline import AudioTimeline
from .virtual_player import (
    IDLE,
    NEXT_REQUESTED,
    PAUSE_REQUESTED,
    PLAY_REQUESTED,
    PREVIOUS_REQUESTED,
    RECOVERING,
    SEEKING,
    SEEK_REQUESTED,
    SOURCE_AIRPLAY,
    SOURCE_DLNA,
    SOURCE_INTERNAL,
    STOPPING,
    TRACK_SWITCHING,
    VirtualPlayer,
    PlaybackState,
    _ResumeTimeline,
    _hms_to_ms,
    _parse_last_change,
    _sniff_image_mime,
)

log = logging.getLogger("state")

#: 兼容既有调用方（测试 / 旧代码）的历史常量别名
_MODE_STOP = MODE_STOP
_MODE_PLAY = MODE_PLAY
_MODE_PAUSE = MODE_PAUSE
_RENDERER_PLAYING = RENDERER_PLAYING
_RENDERER_PAUSED = RENDERER_PAUSED
_RENDERER_STOPPED = RENDERER_STOPPED
_RENDERER_TRANSITIONING = RENDERER_TRANSITIONING
_MAX_ACTION_ERRORS = 5

#: 转发给 DLNA Output 的属性名
_OUTPUT_ATTRS = frozenset({
    "_intent", "_intent_event", "_intent_lock", "_worker", "_converge_dirty",
    "_gen_token", "_renderer_token", "_paused_with_stop", "_action_errors",
    "_uri_reason", "_uri_count", "_uri_log", "_subscriptions",
    "_last_renew_monotonic", "_last_volume_tx", "_last_volume_rx",
    "_silence_timeout_pending", "_base_url", "_http_port",
})

#: 这些名字由 BridgeController 自己持有（不转发）
_OWN_ATTRS = frozenset({
    "config", "registry", "ring", "timeline", "streams", "log", "state", "remote",
    "_lock", "_stop_event", "_output", "_player", "_profile_cache",
})


class BridgeController:
    """组合根：wiring + 生命周期 + 对外查询；状态机在 Virtual Player 里。"""

    def __init__(self, config, registry, ring: PcmRingBuffer, timeline: AudioTimeline,
                 streams, log: Optional[Any] = None) -> None:
        self.config = config
        self.registry = registry
        self.ring = ring
        self.timeline = timeline
        self.streams = streams
        self.log = log or (lambda msg, *a: None)

        #: 共享基础设施：同一把可重入锁、同一个停止事件（两个工作线程共用）
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        #: 唯一对外状态对象（Virtual Player 与 DLNA Output 共享同一实例）
        self.state = PlaybackState()
        self.remote = AirPlayRemoteController(
            log=log, timeout=_float_or(config, "dacp_timeout_seconds", 2.0)
        )
        self._output = DLNAOutput(
            config, registry, ring, timeline, streams,
            state=self.state, lock=self._lock, stop_event=self._stop_event,
            log=log, profile_provider=self._profile_for,
        )
        self._player = VirtualPlayer(
            config, registry, ring, timeline, streams,
            output=self._output, remote=self.remote,
            lock=self._lock, stop_event=self._stop_event, state=self.state, log=log,
        )
        #: Renderer Profile 缓存：(udn, profile)
        self._profile_cache: tuple[str, Optional[RendererProfile]] = ("", None)

    # ------------------------------------------------- 属性转发（历史调用方兼容）
    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        player = self.__dict__.get("_player")
        if player is not None:
            if name in _OUTPUT_ATTRS:
                return getattr(player.output, name)
            try:
                return getattr(player, name)
            except AttributeError:
                pass
            try:
                return getattr(player.output, name)
            except AttributeError:
                pass
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}")

    def __setattr__(self, name: str, value) -> None:
        if name.startswith("__") or name in _OWN_ATTRS or name in self.__dict__:
            object.__setattr__(self, name, value)
            return
        player = self.__dict__.get("_player")
        if player is not None:
            if name in _OUTPUT_ATTRS or name in player.output.__dict__:
                setattr(player.output, name, value)
                return
            if name in player.__dict__:
                setattr(player, name, value)
                return
        object.__setattr__(self, name, value)

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self._player.start()

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self.streams.close_all()
        except Exception:  # noqa: BLE001
            pass
        self._player.stop()

    # ------------------------------------------------------------ Renderer Profile
    def _profile_for(self, record=None) -> RendererProfile:
        """按渲染器身份选择 Profile（配置项 ``renderer_profile`` 可强制覆盖）。"""
        record = record if record is not None else self.registry.selected()
        override = str(self.config.get("renderer_profile") or "")
        if record is None:
            return select_profile(None, override=override, config=self.config)
        udn = getattr(record, "udn", "") or ""
        cached = self._profile_cache
        if cached[0] == udn and cached[1] is not None:
            return cached[1]
        profile = select_profile(record, override=override, config=self.config)
        self._profile_cache = (udn, profile)
        if cached[1] is None or cached[1].name != profile.name:
            log.info("Renderer Profile 选择: udn=%s name=%r model=%r -> %s",
                     udn or "-", getattr(record, "name", ""),
                     getattr(record, "model", ""), profile.name)
        return profile

    @property
    def profile(self) -> RendererProfile:
        """当前选中渲染器的 Profile。"""
        return self._profile_for()

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
        # 切换设备时结束旧会话，并按新设备重新选择 Profile
        self._profile_cache = ("", None)
        self._player.reset_for_renderer_change()

    # ------------------------------------------------------------------ 对外查询
    def status(self) -> dict:
        with self._lock:
            self._player._sync_positions()
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
            profile = self._profile_for(record)
            diagnostics: dict[str, Any] = self._output.diagnostics()
            diagnostics.update(self._player.diagnostics())
            diagnostics["generation"] = self.ring.generation
            return {
                "playback": playback,
                "renderer": renderer_info,
                "timeline": self.timeline.snapshot(),
                "buffer": self.ring.stats(),
                "diagnostics": diagnostics,
                "intent": {"mode": self._output.intent_mode,
                           "token": self._output.intent_token},
                "virtual_player": {
                    "machine_state": self._player.machine_state,
                    "pending_request": playback["pending_request"],
                    "control_source": playback["control_source"],
                    "request_id": playback["request_id"],
                },
                "renderer_profile": profile.to_dict(),
                "reverse_control": self._player.reverse_control_status(),
            }


def _float_or(config, key: str, fallback: float) -> float:
    try:
        value = config.get(key)
    except Exception:  # noqa: BLE001
        return fallback
    if value is None:
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


__all__ = [
    "BridgeController", "VirtualPlayer", "DLNAOutput", "AirPlayRemoteController",
    "PlaybackState", "RendererProfile", "PROFILES", "GENERIC_PROFILE",
    "STOPPED", "PAUSED", "PLAYING", "BUFFERING", "ERROR",
    "IDLE", "SEEKING", "TRACK_SWITCHING", "RECOVERING", "STOPPING",
    "PLAY_REQUESTED", "PAUSE_REQUESTED", "SEEK_REQUESTED",
    "NEXT_REQUESTED", "PREVIOUS_REQUESTED",
    "SOURCE_AIRPLAY", "SOURCE_DLNA", "SOURCE_INTERNAL",
    "_ResumeTimeline", "_Intent",
    # 历史工具函数（从 virtual_player 再导出，保持既有导入路径可用）
    "_sniff_image_mime", "_hms_to_ms", "_parse_last_change",
]
