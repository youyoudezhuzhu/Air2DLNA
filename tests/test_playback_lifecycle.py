"""播放生命周期回归测试 —— 对应 1.0.5 架构约定。

架构约定（唯一权威时间线 = AirPlay 时间线）：

* DLNA 的 ``RelTime`` / ``GetPositionInfo`` / 内部缓冲延迟 / HTTP ``Range`` /
  HTTP 重连 —— 全部只是**观测值**，不得驱动任何换代或 ``SetAVTransportURI``；
* 只有三类原因允许更换媒体生命周期（generation / token / URI）：
  A. 真实 seek（``pfls`` / ``pdis``）
  B. 渲染器链路真的断了（声称在播放却长时间没人拉流）
  C. 播放真的结束（``pend`` 过渡窗口超时 / ``aend``）
* 真机曾经出现的 3~4 秒固定偏差是**渲染器缓冲延迟**，据此重建会让音箱重新淡入，
  听感就是「播放十几秒后声音突然变小又变大」—— 必须不再发生。
* ``pend`` 只是播放流结束，不等价于会话结束：iPhone 拖动进度条后会紧跟新流事件，
  旧实现立刻 Stop DLNA 导致几十秒无声。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "app" / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from air2dlna import state as state_mod  # noqa: E402
from air2dlna.ringbuffer import PcmRingBuffer  # noqa: E402
from air2dlna.timeline import AudioTimeline, monotonic_raw_ns  # noqa: E402

SAMPLE_RATE = 44100
CHANNELS = 2


class _Cfg(dict):
    def get(self, key, default=None):  # noqa: D102
        return dict.get(self, key, default)

    def update(self, changes):  # noqa: D102
        self.update_dict = dict(changes)
        dict.update(self, changes)


class _StubClient:
    def get_transport_info(self):  # noqa: D102
        return {"state": "PLAYING"}

    def get_position_info(self):  # noqa: D102
        return {"rel_time_ms": 0.0, "duration_ms": 226000.0}

    def set_av_transport_uri(self, *a, **kw):  # noqa: D102
        return True

    def play(self, *a, **kw):  # noqa: D102
        return True


class _Record:
    def __init__(self, client=None) -> None:
        self.udn = "uuid:fake"
        self.name = "小爱音箱-测试"
        self.ip = "192.168.1.60"
        self.client = client
        self.model = "S12"
        self.manufacturer = "Mi, Inc."
        self.online = True
        self.supported_mime = "http-get:*:audio/wav:*"
        self.capability_error = ""


class _Registry:
    def __init__(self, record) -> None:
        self._record = record

    def selected(self):  # noqa: D102
        return self._record

    @staticmethod
    def resolve_stream_kind(record, output_format):  # noqa: D102
        return "audio/wav", "wav"


class _Session:
    def __init__(self, token, generation, **kw) -> None:
        self.token = token
        self.generation = generation
        self.closed = False
        self.clients = kw.get("clients", 0)
        self.created_at = kw.get("created_at", time.monotonic())
        self.last_activity = kw.get("last_activity", 0.0)
        self.duration_ms = None
        self.content_type = "audio/wav"
        self.protocol_info = "http-get:*:audio/wav:*"
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self.bits = 16
        self.trailing_bytes = 0
        self.bytes_served = kw.get("bytes_served", 0)
        self.range_requests = 0
        self.last_range_info = ""

    def path(self, prefix="/stream"):  # noqa: D102
        return f"{prefix}/{self.token}.wav"


class _Streams:
    def __init__(self, ring) -> None:
        self.ring = ring
        self.created = []
        self._sessions = {}

    def new_generation(self, kind, duration_ms=None):  # noqa: D102
        token = f"tok-{len(self.created) + 1}"
        session = _Session(token, self.ring.generation)
        self.created.append(token)
        self._sessions[token] = session
        return session

    def get(self, token):  # noqa: D102
        return self._sessions.get(token)

    def update_duration(self, duration_ms):  # noqa: D102
        return None

    def close_all(self):  # noqa: D102
        self.close_calls = getattr(self, "close_calls", 0) + 1
        return None


def _build():
    config = _Cfg({
        "drift_threshold_ms": 1500,
        "preroll_seconds": 0.0,
        "output_format": "auto",
        "http_port": 8788,
        "buffer_seconds": 120,
        "av_offset_ms": 0,
    })
    ring = PcmRingBuffer(SAMPLE_RATE * CHANNELS * 2 * 30, sample_rate=SAMPLE_RATE,
                         channels=CHANNELS)
    timeline = AudioTimeline(sample_rate=SAMPLE_RATE)
    streams = _Streams(ring)
    record = _Record(_StubClient())
    controller = state_mod.BridgeController(config, registry=_Registry(record), ring=ring,
                                            timeline=timeline, streams=streams)
    controller._session_started_at = time.monotonic() - 60
    controller.timeline.on_play_begin()
    return controller, config, ring, timeline, record, streams


def _set_offset(timeline: AudioTimeline, seconds: float) -> None:
    samples = int(seconds * SAMPLE_RATE)
    timeline.on_prgr(f"0/{samples}/10000000 44100")
    timeline.on_phbt(f"{samples}/{monotonic_raw_ns()}")


def _observe(controller, timeline, airplay_s: float, rel_ms: float) -> None:
    """模拟一轮位置轮询：AirPlay 位置 + 渲染器 RelTime，然后观测。"""
    _set_offset(timeline, airplay_s)
    timeline.update_renderer_position(rel_ms)
    controller._observe_renderer_timeline()


class TimelineObservationTests(unittest.TestCase):
    """需求 1~3：DLNA 位置只作观测，绝不驱动重建。"""

    def test_stable_offset_never_rebuilds(self) -> None:
        # 复现真机数值：渲染器稳定落后约 3 秒（内部缓冲/淡入）
        controller, _, ring, timeline, _, streams = _build()
        controller._handle_play(False)          # pbeg：正常开始播放
        created_after_pbeg = len(streams.created)
        generation_before = ring.generation

        for i in range(20):               # 约 1 分钟的轮询
            _observe(controller, timeline, 10.0 + i * 3.0, (10.0 + i * 3.0) * 1000 - 3000)

        self.assertEqual(generation_before, ring.generation, "固定缓冲延迟不得触发换代")
        self.assertEqual(created_after_pbeg, len(streams.created), "不得新建流 token")
        self.assertEqual(0, controller._uri_count, "不得出现 SetAVTransportURI")
        self.assertEqual("pbeg", controller._last_rebuild_reason, "轮询观测期间不应产生新的重建原因")
        self.assertIsNotNone(controller._rel_offset_ms, "应当记录偏移用于诊断")

    def test_growing_offset_never_rebuilds(self) -> None:
        # 偏移持续增长（真漂移的形态）：仍只记诊断，不重建
        controller, _, ring, timeline, _, streams = _build()
        controller._handle_play(False)
        generation_before = ring.generation
        created = len(streams.created)

        for i in range(20):
            # 渲染器位置几乎不动 → 偏移每小时轮 +3 秒地增长
            _observe(controller, timeline, 10.0 + i * 3.0, 10.0 * 1000)

        self.assertEqual(generation_before, ring.generation, "真漂移也不应自动重建")
        self.assertEqual(created, len(streams.created), "不得新建流 token")
        self.assertEqual(0, controller._uri_count)

    def test_http_range_and_reconnect_never_rebuild(self) -> None:
        # 需求 3：同一 token 下反复 GET / Range / reconnect 属于传输层细节
        controller, _, ring, timeline, _, streams = _build()
        controller._handle_play(False)
        generation_before = ring.generation
        created = len(streams.created)
        session = streams.get(controller._gen_token)

        for range_start in (0, 44, 2291832, 3103404, 5484156, 0):
            session.range_requests += 1
            session.last_range_info = f"start={range_start} req={session.range_requests}"
            controller._observe_renderer_timeline()

        self.assertEqual(generation_before, ring.generation, "HTTP Range 不得触发换代")
        self.assertEqual(created, len(streams.created), "HTTP 重连不得新建流 token")
        self.assertEqual(0, controller._uri_count)

    def test_diagnostics_exposed_in_status(self) -> None:
        controller, _, _, timeline, _, _ = _build()
        controller._handle_play(False)
        _observe(controller, timeline, 12.0, 9000.0)
        status = controller.status()
        self.assertIn("diagnostics", status)
        diag = status["diagnostics"]
        for key in ("generation", "renderer_rel_time_ms", "rel_offset_ms", "rel_rate",
                    "uri_count", "uri_log", "last_rebuild_reason", "awaiting_new_stream"):
            self.assertIn(key, diag, f"诊断字段缺失: {key}")


class PendTransitionTests(unittest.TestCase):
    """需求 5~6：pend 是播放流结束，不是会话结束。"""

    def test_pend_enters_transition_without_stopping(self) -> None:
        controller, _, ring, _, _, streams = _build()
        controller._handle_play(False)
        generation_before = ring.generation
        token_before = controller._gen_token
        self.assertTrue(token_before)

        controller._handle_play_stream_end("播放流结束")

        self.assertTrue(controller._awaiting_new_stream, "应进入过渡态")
        self.assertNotEqual(state_mod.STOPPED, controller.state.state, "不得立即置为 STOPPED")
        self.assertEqual(generation_before, ring.generation, "不得 flush 缓冲")
        self.assertEqual(token_before, controller._gen_token, "不得清空 token")
        self.assertEqual(0, getattr(streams, "close_calls", 0), "不得关闭流")

    def test_pbeg_with_continuous_position_reuses_session(self) -> None:
        """暂停恢复 / seek 之后位置连续：沿用会话，绝不重建。

        AirPlay 2 在暂停恢复与 seek 之后都会发 ``pend`` + ``pbeg``。若把每个
        ``pbeg`` 都当新会话来重建，音箱会重新缓冲 → 真机表现为卡顿甚至停止。
        """
        controller, _, ring, timeline, _, streams = _build()
        controller._handle_play(False)
        generation_before = ring.generation
        created_before = len(streams.created)
        _observe(controller, timeline, 30.0, 27000.0)      # 位置偏差仅约 3 秒（缓冲延迟量级）
        controller._handle_play_stream_end("播放流结束")

        controller._handle_play(False)                     # 恢复播放

        self.assertFalse(controller._awaiting_new_stream, "过渡态应被取消")
        self.assertEqual(generation_before, ring.generation, "位置连续时不得换代")
        self.assertEqual(created_before, len(streams.created), "不得更换流 URI")
        self.assertEqual(0, controller._uri_count, "不得出现 SetAVTransportURI")

    def test_pbeg_with_jumped_position_rebuilds(self) -> None:
        """换曲 / 跳到别处：曲目位置真跳变时必须换代（否则音箱还在播旧位置的数据）。"""
        controller, _, ring, timeline, _, streams = _build()
        controller._handle_play(False)
        generation_before = ring.generation
        created_before = len(streams.created)
        _observe(controller, timeline, 30.0, 27000.0)
        controller._handle_play_stream_end("播放流结束")     # 记录边界位置 30 秒
        _observe(controller, timeline, 300.0, 1000.0)        # 位置跳到 300 秒（换曲）

        controller._handle_play(False)

        self.assertEqual(generation_before + 1, ring.generation, "位置跳变应换代")
        self.assertEqual(created_before + 1, len(streams.created), "应新建流 URI")
        self.assertEqual("pbeg", controller._last_rebuild_reason)

    def test_pbeg_right_after_seek_does_not_rebuild_again(self) -> None:
        """真实 seek 已经换代，紧随的 pbeg 不许再来一次（否则音箱重复缓冲）。"""
        controller, _, ring, timeline, _, streams = _build()
        controller._handle_play(False)
        _observe(controller, timeline, 30.0, 27000.0)
        controller._handle_flush("12345")                    # 真实 seek → 换代

        generation_after_seek = ring.generation
        created_after_seek = len(streams.created)
        controller._handle_play(False)                       # 紧随其后的 pbeg

        self.assertEqual(generation_after_seek, ring.generation, "seek 后的 pbeg 不得再换代")
        self.assertEqual(created_after_seek, len(streams.created), "不得再次更换 URI")

    def test_generation_change_marks_pending_reanchor(self) -> None:
        """换代后必须标记「基准待重锚」，等随后的 prgr 用真位置修正。

        pbeg 换代时 prgr 往往还没到，基准会退化成 0 → 位置偏差恒等于曲目绝对位置
        （真机实测 138 秒），既污染诊断也误导连续性判断。
        """
        controller, _, ring, timeline, _, streams = _build()
        with controller._lock:
            controller._pending_reanchor = False

        controller._handle_play(False)                       # pbeg → 换代

        self.assertTrue(controller._pending_reanchor, "换代后应标记基准待重锚")
        _set_offset(timeline, 120.0)                         # 随后 prgr 上报真实位置
        controller._after_progress()
        self.assertAlmostEqual(120000.0, timeline.generation_offset_ms, delta=1000.0,
                               msg="prgr 到达后代偏移应被修正为真实位置")

    def test_resume_intent_forbids_set_uri(self) -> None:
        """pres（暂停恢复）路径也不得允许重设 URI。

        真机走的就是这条分支（AirPlay 2 暂停恢复发 `pres`）：渲染器收到 Pause 后
        自行转入 STOPPED，若此时重设 URI 就会从资源开头播放。
        """
        controller, _, ring, _, _, _ = _build()
        controller._handle_play(False)
        controller._handle_play(True)          # pres：暂停恢复

        self.assertFalse(controller._intent.set_uri, "暂停恢复不得允许重设 URI")
        self.assertEqual(state_mod._MODE_PLAY, controller._intent.mode)

    def test_reuse_path_never_resets_uri(self) -> None:
        """复用同一个 generation 时只能发 Play —— 重设 URI 会让渲染器从头播放。"""
        controller, _, ring, timeline, _, streams = _build()
        controller._handle_play(False)
        record = controller.registry.selected()
        client = record.client
        calls: list[str] = []
        client.play = lambda *a, **kw: (calls.append("play"), True)[1]
        client.set_av_transport_uri = lambda *a, **kw: (calls.append("set_uri"), True)[1]
        controller._renderer_token = controller._gen_token
        with controller._lock:
            controller.state.renderer_state = state_mod._RENDERER_STOPPED

        controller._do_play(record, client, controller._gen_token, set_uri=False)

        self.assertEqual(["play"], calls, "复用路径只能发 Play，绝不能重设 URI")

    def test_pcm_resumes_cancel_transition(self) -> None:
        controller, _, _, _, _, _ = _build()
        controller._handle_play(False)
        controller._handle_play_stream_end("播放流结束")
        controller.on_audio_bytes(b"\x00" * 4096)
        self.assertFalse(controller._awaiting_new_stream, "仍有 PCM 时应退出过渡态")

    def test_transition_timeout_ends_session(self) -> None:
        controller, _, _, _, _, _ = _build()
        controller._handle_play(False)
        controller._handle_play_stream_end("播放流结束")
        controller._transition_deadline = time.monotonic() - 1.0   # 模拟窗口已过

        controller._check_transition_timeout()

        self.assertEqual(state_mod.STOPPED, controller.state.state, "超时应真正停止")
        self.assertEqual("", controller._gen_token, "应清空 token")

    def test_aend_still_ends_immediately(self) -> None:
        controller, _, _, _, _, _ = _build()
        controller._handle_play(False)
        controller._handle_session_end("播放结束")
        self.assertEqual(state_mod.STOPPED, controller.state.state)


class RebuildSourceTests(unittest.TestCase):
    """需求 4：只允许三类原因更换媒体生命周期。"""

    def test_seek_rebuilds_exactly_once(self) -> None:
        controller, _, ring, _, _, streams = _build()
        controller._handle_play(False)
        generation_before = ring.generation
        created_before = len(streams.created)

        controller._handle_flush("12345")   # pfls：真实 seek

        self.assertEqual(generation_before + 1, ring.generation, "seek 应换代一次")
        self.assertEqual(created_before + 1, len(streams.created), "seek 只允许一次新流")
        self.assertEqual("flush", controller._last_rebuild_reason)

    def test_stalled_renderer_rebuilds(self) -> None:
        # 渲染器声称在播放，但 8 秒以上没人拉流 → 允许兜底重建
        controller, _, ring, _, _, streams = _build()
        controller._handle_play(False)
        with controller._lock:
            controller.state.state = state_mod.PLAYING
        controller._renderer_token = controller._gen_token   # 模拟收敛线程已下发
        session = streams.get(controller._gen_token)
        session.clients = 0
        session.last_activity = time.monotonic() - 30.0
        controller._last_stall_rebuild = 0.0
        generation_before = ring.generation

        controller._check_renderer_stream()

        self.assertEqual(generation_before + 1, ring.generation)
        self.assertEqual("stalled", controller._last_rebuild_reason)

    def test_healthy_stream_is_not_touched(self) -> None:
        controller, _, ring, _, _, streams = _build()
        controller._handle_play(False)
        with controller._lock:
            controller.state.state = state_mod.PLAYING
        controller._renderer_token = controller._gen_token
        session = streams.get(controller._gen_token)
        session.clients = 1                  # 有客户端在拉流
        session.last_activity = time.monotonic()
        generation_before = ring.generation

        controller._check_renderer_stream()

        self.assertEqual(generation_before, ring.generation, "正常拉流时不得打扰")

    def test_normal_playback_uses_single_media_resource(self) -> None:
        """验收测试 1 的单元化：30 分钟正常播放只允许一个 generation/token。"""
        controller, _, ring, timeline, _, streams = _build()
        controller._handle_play(False)
        generation_before = ring.generation
        created_after_pbeg = len(streams.created)

        for i in range(60):                  # 60 × 30s = 30 分钟
            _observe(controller, timeline, 30.0 + i * 30.0,
                     (30.0 + i * 30.0) * 1000 - 3200)
            controller._check_renderer_stream()

        self.assertEqual(created_after_pbeg, len(streams.created),
                         "30 分钟正常播放不得再新建流 token")
        self.assertEqual(generation_before, ring.generation, "不得换代")
        self.assertEqual(0, controller._uri_count, "不得出现 SetAVTransportURI")


class PauseRecoveryTests(unittest.TestCase):
    """方案 A（暂停恢复）：保持 generation/URI，把渲染器重连的 Range 0 映射到暂停位置。

    真机实测：小爱音箱把 UPnP Pause 执行成 Stop；Stop 后只发 Play 会"假播放"
    （报告 PLAYING、继续拉流、但无声）；重设同一 URI 又会从资源 0 点重播。
    方案 A 利用它 Stop 后会自己重连 HTTP（Range: bytes=0-）的行为。
    """

    @staticmethod
    def _fill(ring, seconds: float = 40.0) -> None:
        ring.append(b"\x00" * int(SAMPLE_RATE * CHANNELS * 2 * seconds))

    def test_pause_records_recovery_info(self) -> None:
        controller, _, ring, timeline, _, _ = _build()
        controller._handle_play(False)
        self._fill(ring)
        _observe(controller, timeline, 30.0, 27000.0)

        controller._handle_pause()

        self.assertIsNotNone(controller._paused_airplay_position_ms, "应记录暂停位置")
        self.assertIsNotNone(controller._paused_ring_offset, "应换算出当代内字节偏移")
        self.assertEqual(ring.generation, controller._paused_generation)
        self.assertTrue(controller._pause_position_available(), "暂停位置应在缓冲窗口内")

    def test_pause_recovery_starts_on_resume_without_rebuild(self) -> None:
        controller, _, ring, timeline, _, streams = _build()
        controller._handle_play(False)
        self._fill(ring)
        _observe(controller, timeline, 30.0, 27000.0)
        controller._handle_pause()
        with controller._lock:
            controller._pause_recovery_pending = True     # 轮询检测到渲染器 STOPPED
        generation_before = ring.generation
        created_before = len(streams.created)

        controller._handle_play(True)                      # pres：iPhone 恢复

        self.assertGreater(controller._pause_recovery_deadline, 0.0, "方案 A 应已启动")
        self.assertEqual(generation_before, ring.generation, "不得换代")
        self.assertEqual(created_before, len(streams.created), "不得更换 URI")
        session = streams.get(controller._gen_token)
        self.assertIsNotNone(session.recovery_byte_offset, "应设置 Range 0 的映射偏移")
        self.assertEqual(controller._paused_ring_offset, session.recovery_byte_offset)
        self.assertTrue(controller._start_pause_recovery() is False or True)  # 幂等性不炸

    def test_pause_recovery_success_clears_state(self) -> None:
        controller, _, ring, timeline, _, streams = _build()
        controller._handle_play(False)
        self._fill(ring)
        _observe(controller, timeline, 30.0, 27000.0)
        controller._handle_pause()
        with controller._lock:
            controller._pause_recovery_pending = True
        controller._handle_play(True)
        session = streams.get(controller._gen_token)
        session.recovery_applied = True                    # 渲染器用上了映射
        session.clients = 1

        controller._check_pause_recovery()

        self.assertFalse(controller._pause_recovery_pending, "成功后应清除待恢复状态")
        self.assertEqual(0.0, controller._pause_recovery_deadline)
        self.assertIsNone(session.recovery_byte_offset, "成功后应撤掉映射")

    def test_pause_recovery_timeout_falls_back(self) -> None:
        controller, _, ring, timeline, _, streams = _build()
        controller._handle_play(False)
        self._fill(ring)
        _observe(controller, timeline, 30.0, 27000.0)
        controller._handle_pause()
        with controller._lock:
            controller._pause_recovery_pending = True
        controller._handle_play(True)
        generation_before = ring.generation
        created_before = len(streams.created)
        controller._pause_recovery_deadline = time.monotonic() - 1.0   # 窗口已过

        controller._check_pause_recovery()

        self.assertEqual(generation_before + 1, ring.generation, "超时应换代")
        self.assertEqual(created_before + 1, len(streams.created), "超应建新 URI")
        self.assertEqual("pause-recovery-fallback", controller._last_rebuild_reason)

    def test_pause_recovery_skipped_when_position_out_of_window(self) -> None:
        controller, _, ring, timeline, _, _ = _build()
        controller._handle_play(False)
        self._fill(ring)
        _observe(controller, timeline, 30.0, 27000.0)
        controller._handle_pause()
        with controller._lock:
            controller._pause_recovery_pending = True
            controller._paused_ring_offset = 10 ** 9        # 暂停位置已被覆盖

        started = controller._start_pause_recovery()

        self.assertFalse(started, "位置超出缓冲窗口时不得启用方案 A")

    def test_watchdog_suppressed_during_recovery(self) -> None:
        controller, _, ring, _, _, streams = _build()
        controller._handle_play(False)
        controller._renderer_token = controller._gen_token
        session = streams.get(controller._gen_token)
        session.clients = 0
        session.last_activity = time.monotonic() - 30.0
        with controller._lock:
            controller.state.state = state_mod.PLAYING
            controller._pause_recovery_pending = True
        generation_before = ring.generation

        controller._check_renderer_stream()

        self.assertEqual(generation_before, ring.generation,
                         "方案 A 期间不得触发兜底重建（会打断恢复）")

    def test_seek_clears_pause_recovery(self) -> None:
        controller, _, ring, timeline, _, _ = _build()
        controller._handle_play(False)
        self._fill(ring)
        _observe(controller, timeline, 30.0, 27000.0)
        controller._handle_pause()
        with controller._lock:
            controller._pause_recovery_pending = True

        controller._handle_flush("999")                    # 真实 seek

        self.assertFalse(controller._pause_recovery_pending, "seek 应清除暂停恢复状态")


class StreamRecoveryMappingTests(unittest.TestCase):
    """服务端映射：暂停恢复期间 Range: bytes=0- 从暂停位置读数据（WAV header 语义不变）。"""

    def test_serve_maps_zero_range_to_recovery_offset(self) -> None:
        import io

        from air2dlna.stream import StreamManager

        ring = PcmRingBuffer(SAMPLE_RATE * CHANNELS * 2 * 10, sample_rate=SAMPLE_RATE,
                            channels=CHANNELS)
        ring.append(b"\x00" * (SAMPLE_RATE * CHANNELS * 2 * 4))       # 前 4 秒：静音
        ring.append(b"\xab" * (SAMPLE_RATE * CHANNELS * 2 * 4))       # 之后：非静音
        manager = StreamManager(ring)
        session = manager.new_generation("l16")
        session.total_bytes = 64                                       # 只发一点就结束
        session.recovery_byte_offset = SAMPLE_RATE * CHANNELS * 2 * 5  # 映射到 5 秒处
        out = io.BytesIO()

        manager.serve(session, out, range_start=0)

        data = out.getvalue()
        self.assertTrue(session.recovery_applied, "映射应被标记为已生效")
        self.assertTrue(data.startswith(b"\xab"), "应从映射位置（而非本代起点）开始发送")

    def test_serve_without_recovery_keeps_generation_start(self) -> None:
        import io

        from air2dlna.stream import StreamManager

        ring = PcmRingBuffer(SAMPLE_RATE * CHANNELS * 2 * 10, sample_rate=SAMPLE_RATE,
                            channels=CHANNELS)
        ring.append(b"\x00" * (SAMPLE_RATE * CHANNELS * 2 * 4))
        ring.append(b"\xab" * (SAMPLE_RATE * CHANNELS * 2 * 4))
        manager = StreamManager(ring)
        session = manager.new_generation("l16")
        session.total_bytes = 64
        out = io.BytesIO()

        manager.serve(session, out, range_start=0)

        self.assertFalse(session.recovery_applied)
        self.assertTrue(out.getvalue().startswith(b"\x00"), "未启用映射时行为不变")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
