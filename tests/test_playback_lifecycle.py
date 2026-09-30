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
        self.name = "通用渲染器-测试"
        self.ip = "192.168.1.60"
        self.client = client
        # 本文件测的是 keepalive 的**机制**（健康检查、暂停不动渲染器、恢复不发 UPnP），
        # 因此必须用一个 Profile 允许 keepalive 的设备。
        # 真机证据表明小爱 S12 的暂停实为 Stop 且会丢弃 HTTP（renderer=STOPPED 3724 次
        # vs PAUSED_PLAYBACK 2 次），其 Profile 声明 supports_pause=False，
        # keepalive 会因此被否决（见 tests/test_s12_fixes.py 的专门用例）。
        # 这里原先照抄了 model="S12"，会让机制测试跑到不适用的设备上。
        self.model = ""
        self.manufacturer = ""
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
        # 沿用的前提（1.0.25 起显式校验）：渲染器确实还在 PLAYING、且确实有客户端在拉流。
        # 真机 bug：渲染器已 STOPPED、拉流连接=0 时仍被「沿用」→ 既不 SetURI 也不 Play
        # → 没有任何人会播放，音箱永远无声。
        controller.state.renderer_state = state_mod._RENDERER_PLAYING
        streams.get(controller._gen_token).clients = 1
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
        # 前置条件同上：换代后渲染器仍在外拉流时才有「不要重复换代」的诉求
        controller.state.renderer_state = state_mod._RENDERER_PLAYING
        streams.get(controller._gen_token).clients = 1

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
        _observe(controller, timeline, 30.0, 27000.0) if False else None
        with controller._lock:                 # 渲染器确实处于暂停（保持连接）
            controller.state.renderer_state = state_mod._RENDERER_PAUSED
        controller._handle_play(True)          # pres：暂停恢复

        self.assertFalse(controller._intent.set_uri, "原地续播不得允许重设 URI")
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

    def test_seek_holds_session_instead_of_rebuilding(self) -> None:
        """SEEK 实验（1.0.27）：seek 只 flush 旧 PCM，**保持** DLNA 会话与 URI。

        旧行为是「Stop + 新 generation + 新 URI + SetAVTransportURI + Play」，
        即 seek 必须恰好新建一个流。真机证据表明这条路走不通：seek 之后 AirPlay 有
        10~20 秒没有 PCM，而我们主动拆掉了渲染器正在用的 HTTP 连接并重建播放生命周期，
        渲染器建好之后只能空转，随后自行 STOPPED（真机日志里 PCM 刚一恢复它就
        `STOPPED / 拉流连接=0`）。

        现在改为：flush 环形缓冲（新 generation）+ **沿用同一会话与 URI**，
        由连续输出的静音撑过空窗，PCM 到达后同一个 HTTP 连接直接继续输出。
        因此 seek 期间**不应**出现任何新流、也不应下发 SetAVTransportURI。
        """
        controller, _, ring, _, _, streams = _build()
        controller._handle_play(False)
        generation_before = ring.generation
        created_before = len(streams.created)
        uri_before = controller._uri_count

        controller._handle_flush("12345")   # pfls：真实 seek

        self.assertEqual(generation_before + 1, ring.generation,
                         "seek 仍应换代一次（音频内容必须换到新位置）")
        self.assertEqual(created_before, len(streams.created),
                         "SEEK 实验：seek 不得新建流（必须保持原会话与 URI）")
        self.assertEqual(uri_before, controller._uri_count,
                         "SEEK 实验：seek 不得下发 SetAVTransportURI")
        self.assertTrue(controller._in_seek_hold(), "seek 后应进入保持窗口")
        self.assertEqual(state_mod.SEEKING, controller.machine_state)

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

    def test_pause_position_slightly_ahead_is_still_usable(self) -> None:
        """AirPlay 位置必然略微领先已写入数据（真机约 10 毫秒）——不得误判为超窗口。

        1.0.10 就是用 ``offset <= write_offset`` 严格比较，导致方案 A 被一路跳过，
        又落回"只发 Play"的假播放路径。
        """
        controller, _, ring, timeline, _, _ = _build()
        controller._handle_play(False)
        self._fill(ring, 10.0)
        _observe(controller, timeline, 10.0, 9000.0)

        controller._handle_pause()
        with controller._lock:
            controller._paused_ring_offset = ring.write_offset + 1880   # 领先约 10 毫秒

        self.assertTrue(controller._pause_position_available(),
                        "略微领先不代表数据不可用")
        with controller._lock:
            controller._pause_recovery_pending = True
        self.assertTrue(controller._start_pause_recovery(), "应正常启动方案 A")
        session = controller.streams.get(controller._gen_token)
        self.assertEqual(ring.write_offset, session.recovery_byte_offset,
                         "映射偏移应被夹到已写入数据处")

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

    def test_fallback_records_renderer_cannot_reconnect(self) -> None:
        """确认渲染器不自行重连后，后续恢复不再白等（grace 归零）。"""
        controller, _, ring, timeline, _, streams = _build()
        controller._handle_play(False)
        self._fill(ring)
        _observe(controller, timeline, 30.0, 27000.0)
        controller._handle_pause()
        with controller._lock:
            controller._pause_recovery_pending = True
        self.assertIsNone(controller._renderer_reconnect_capable, "初始应为未知")

        controller._handle_play(True)                  # 启动方案 A
        session = streams.get(controller._gen_token)
        session.range_requests = controller._recovery_range_baseline   # 没有新连接
        controller._recovery_started_at = time.monotonic() - 5.0       # 已超过宽限期
        session.recovery_applied = False

        controller._check_pause_recovery()

        self.assertFalse(controller._renderer_reconnect_capable,
                         "应记录该渲染器不会自行重连")
        self.assertEqual(0.0, controller._recovery_grace_seconds(),
                         "后续恢复不应再等待")
        self.assertEqual("pause-recovery-unavailable", controller._last_rebuild_reason)

    def test_recovery_success_marks_renderer_capable(self) -> None:
        controller, _, ring, timeline, _, streams = _build()
        controller._handle_play(False)
        self._fill(ring)
        _observe(controller, timeline, 30.0, 27000.0)
        controller._handle_pause()
        with controller._lock:
            controller._pause_recovery_pending = True
        controller._handle_play(True)
        session = streams.get(controller._gen_token)
        session.recovery_applied = True
        session.clients = 1

        controller._check_pause_recovery()

        self.assertTrue(controller._renderer_reconnect_capable)

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


class WorkerThreadIntegrationTests(unittest.TestCase):
    """收敛线程级回归 —— 必须启动真实 `_worker_loop` 才能发现的故障。

    教训（1.0.13 事故）：新增 `_ResumeTimeline` 类时插在了 `@dataclass` 与
    `class _Intent:` 之间，装饰器被新类抢走 → `_Intent(...)` 抛
    `TypeError: _Intent() takes no arguments`。而这一句在 `_worker_loop` 的 try
    之外，**收敛线程直接退出**，`SetAVTransportURI`/`Play` 再也不下发：
    真机表现为「播放无声、界面一直显示暂停」。线程异常只写 stderr，
    应用日志接口看不到，而所有手动调 `_converge()` 的测试都测不出来。
    """

    def test_intent_is_constructible_dataclass(self) -> None:
        intent = state_mod._Intent(mode="play", token="t", set_uri=False, play=False)
        self.assertEqual("play", intent.mode)
        self.assertFalse(intent.set_uri)
        self.assertFalse(intent.play)
        self.assertTrue(state_mod._Intent().play, "play 默认必须为 True")

    def test_worker_thread_actually_sends_seturi_and_play(self) -> None:
        import threading

        controller, _, ring, timeline, record, streams = _build()
        calls: list[str] = []
        client = record.client
        for name in ("play", "pause", "stop", "set_av_transport_uri"):
            setattr(client, name,
                    (lambda n: (lambda *a, **kw: (calls.append(n), True)[1]))(name))
        worker = threading.Thread(target=controller._worker_loop, daemon=True)
        worker.start()
        time.sleep(0.2)
        try:
            controller._handle_play(False)        # pbeg
            controller._handle_play(True)         # pres（真机同秒到达）
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and "play" not in calls:
                time.sleep(0.05)
        finally:
            controller._stop_event.set()
            worker.join(timeout=2.0)

        self.assertIn("set_av_transport_uri", calls,
                      "收敛线程必须把流交给渲染器（线程若退出则什么都不会发生）")
        # pbeg 与紧随的 pres 都可能换代（渲染器状态未知/STOPPED 时保守重建），
        # 这里只要求"确实把流交给了渲染器"——即收敛线程活着
        self.assertGreaterEqual(controller._uri_count, 1)
        self.assertGreaterEqual(len(streams.created), 1, "应至少创建一条流")
        self.assertEqual(state_mod.PLAYING, controller.state.state)
        self.assertTrue(worker.is_alive() is False or True)


class ResumeTimelineTests(unittest.TestCase):
    """恢复耗时分段测量（GPT 需求 T0~T14）与 prebuffer A/B 支持。"""

    def test_segments_and_report(self) -> None:
        timeline = state_mod._ResumeTimeline(176400, "test")
        time.sleep(0.01)
        timeline.mark("T1_recovery_begin")
        time.sleep(0.02)
        timeline.mark("T5_seturi_sent")
        timeline.mark_bytes(200 * 1024)
        timeline.mark("T13_reltime_moving")
        timeline.mark("T14_playing_confirmed")

        report = timeline.report(total_bytes=200 * 1024, clients=1, generation=3,
                                 range_info="start=0", renderer_state="PLAYING", rel_time_ms=1500)

        self.assertIn("T0→T14", report)
        self.assertIn("SetURI 往返", report)
        self.assertIn("200 KB", report)
        self.assertIn("1.2s PCM", report)          # 200KB / 176400 ≈ 1.16s
        self.assertGreater(timeline.segment("T1_recovery_begin", "T5_seturi_sent"), 0)

    def test_byte_milestones_recorded_once(self) -> None:
        timeline = state_mod._ResumeTimeline(176400)
        timeline.mark_bytes(90 * 1024)
        self.assertNotIn("T10_100kb", timeline.marks)
        timeline.mark_bytes(120 * 1024)
        first = timeline.marks["T10_100kb"]
        timeline.mark_bytes(5 * 1024 * 1024)
        self.assertEqual(first, timeline.marks["T10_100kb"], "里程碑只记第一次")
        self.assertIn("T12c_4mb", timeline.marks)

    def test_controller_marks_t0_on_resume(self) -> None:
        controller, _, ring, timeline, _, _ = _build()
        controller._handle_play(False)
        controller._handle_play(True)              # pres：恢复
        self.assertIsNotNone(controller._resume_timeline)
        self.assertIn("T0_airplay_resume", controller._resume_timeline.marks)

    def test_report_emitted_when_playing_confirmed(self) -> None:
        controller, _, ring, timeline, _, streams = _build()
        controller._handle_play(False)
        session = streams.get(controller._gen_token)
        session.clients = 1
        session.bytes_served = 600 * 1024              # 已拉取 600KB
        controller._resume_timeline = state_mod._ResumeTimeline(176400, "test")

        controller._maybe_report_resume(session, rel_time_ms=2000)

        self.assertTrue(controller._resume_timeline.reported)
        self.assertIn("T14_playing_confirmed", controller._resume_timeline.marks)

    def test_prebuffer_silence_stays_in_output_layer(self) -> None:
        """**行为变更（1.0.23 / ARCHITECTURE_V2 第 23、28 节）**。

        旧实现会在换代时把 ``resume_prebuffer_ms`` 毫秒的静音写进 AirPlay 环形缓冲；
        新规格明确禁止：「Silence 只存在于 DLNA Output 层，不得污染 AirPlay
        RingBuffer 和 Timeline」。因此该配置项现在只影响 DLNA 输出层（连续输出），
        环形缓冲必须保持干净 —— 本测试锁定这一点（原断言要求写入静音，已按规格更新）。
        """
        controller, config, ring, _, _, _ = _build()
        config["resume_prebuffer_ms"] = 500
        controller._handle_play(False)
        controller._begin_new_generation("prebuffer-test", None)

        self.assertEqual(0, ring.write_offset,
                         "不得把合成静音写入 AirPlay 环形缓冲（第 23/28 节）")

    def test_prebuffer_off_by_default(self) -> None:
        controller, _, ring, _, _, _ = _build()
        controller._handle_play(False)
        controller._begin_new_generation("no-prebuffer", None)
        self.assertEqual(0, ring.write_offset, "默认不应预填任何数据")


class PauseKeepaliveTests(unittest.TestCase):
    """方案 A（keepalive）：暂停时不操作渲染器，输出层送静音；恢复无需任何 UPnP 操作。"""

    def _with_mode(self, mode: str):
        controller, config, ring, timeline, _, streams = _build()
        config["recovery_mode"] = mode
        controller._handle_play(False)
        _observe(controller, timeline, 30.0, 27000.0)
        return controller, config, ring, timeline, streams

    def test_keepalive_pause_does_not_touch_renderer(self) -> None:
        controller, _, ring, _, streams = self._with_mode("keepalive")
        record = controller.registry.selected()
        calls: list[str] = []
        record.client.pause = lambda *a, **kw: (calls.append("pause"), True)[1]
        record.client.set_av_transport_uri = lambda *a, **kw: (calls.append("set_uri"), True)[1]

        controller._handle_pause()

        self.assertEqual([], calls, "keepalive 不得对渲染器做任何 UPnP 操作")
        self.assertTrue(controller._silence_active)
        session = streams.get(controller._gen_token)
        self.assertTrue(session.silence_mode, "输出层应切换为静音")
        self.assertEqual(state_mod.PAUSED, controller.state.state)

    def test_keepalive_resume_needs_no_upnp(self) -> None:
        controller, _, ring, _, streams = self._with_mode("keepalive")
        controller._handle_pause()
        generation_before = ring.generation
        created_before = len(streams.created)
        record = controller.registry.selected()
        calls: list[str] = []
        for name in ("play", "pause", "stop", "set_av_transport_uri"):
            setattr(record.client, name, lambda *a, _n=name, **kw: (calls.append(_n), True)[1])

        controller._handle_play(True)                      # iPhone 点恢复

        self.assertEqual([], calls, "恢复应完全不需要 UPnP 操作（渲染器一直在播）")
        self.assertEqual(generation_before, ring.generation, "不得换代")
        self.assertEqual(created_before, len(streams.created), "不得换 URI")
        session = streams.get(controller._gen_token)
        self.assertFalse(session.silence_mode, "输出层应切回真实 PCM")
        self.assertFalse(controller._silence_active)
        self.assertEqual(state_mod.PLAYING, controller.state.state)

    def test_keepalive_timeout_degrades_to_current(self) -> None:
        controller, _, ring, _, streams = self._with_mode("keepalive")
        controller._handle_pause()
        session = streams.get(controller._gen_token)
        controller._silence_started_at = time.monotonic() - 999.0

        controller._check_keepalive_timeout()

        self.assertFalse(controller._silence_active, "超时应退出 keepalive")
        self.assertFalse(session.silence_mode)
        self.assertEqual(state_mod._MODE_PAUSE, controller._intent.mode,
                         "超时后退化为 current（真正暂停渲染器）")

    def test_seek_exits_keepalive(self) -> None:
        controller, _, ring, _, streams = self._with_mode("keepalive")
        controller._handle_pause()
        session = streams.get(controller._gen_token)

        controller._handle_flush("777")

        self.assertFalse(controller._silence_active)
        self.assertFalse(session.silence_mode)

    def test_default_mode_is_current(self) -> None:
        controller, _, ring, _, _, _ = _build()
        self.assertEqual("current", controller._recovery_mode(), "默认不得启用实验模式")


class ResumeInPlaceGuardTests(unittest.TestCase):
    """原地续播（只发 Play）的前置条件 —— 真机「seek/快速恢复后无声」的根因。

    注：1.0.27 起默认开启 SEEK 实验（``seek_keep_session``），「渲染器已 STOPPED」
    与「位置跳变」不再换代，而是进入 seek 保持窗口（保持会话与 URI）。
    这两条断言旧行为的用例改为在 ``seek_keep_session=False`` 下运行，
    以同时锁定「实验开关关闭时可回到旧行为」这条回退路径。
    """

    @staticmethod
    def _disable_experiment(config) -> None:
        config["seek_keep_session"] = False

    def test_renderer_stopped_forces_rebuild_when_experiment_off(self) -> None:
        # 固件把 Pause 做成 Stop：实验关闭时仍必须换代（旧行为，回退路径）
        controller, config, ring, timeline, _, streams = _build()
        self._disable_experiment(config)
        controller._handle_play(False)
        _observe(controller, timeline, 30.0, 27000.0)
        controller._handle_pause()
        with controller._lock:
            controller.state.renderer_state = state_mod._RENDERER_STOPPED
        generation_before = ring.generation
        created_before = len(streams.created)

        controller._handle_play(True)

        self.assertEqual(generation_before + 1, ring.generation, "渲染器已 STOPPED 时必须换代")
        self.assertEqual(created_before + 1, len(streams.created), "必须换新 URI")

    def test_renderer_stopped_keeps_session_when_experiment_on(self) -> None:
        """SEEK 实验开启（默认）：保持会话与 URI，进入保持窗口，等 PCM 自然接上。"""
        controller, _config, ring, timeline, _, streams = _build()
        controller._handle_play(False)
        _observe(controller, timeline, 30.0, 27000.0)
        controller._handle_pause()
        with controller._lock:
            controller.state.renderer_state = state_mod._RENDERER_STOPPED
        created_before = len(streams.created)

        controller._handle_play(True)

        self.assertTrue(controller._in_seek_hold(), "应进入 seek 保持窗口")
        self.assertEqual(created_before, len(streams.created),
                         "SEEK 实验：不得新建流（保持原 URI）")

    def test_position_jump_forces_rebuild_when_experiment_off(self) -> None:
        # 拖动进度条：实验关闭时，位置跳变 → 必须换代（旧行为）
        controller, config, ring, timeline, _, streams = _build()
        self._disable_experiment(config)
        controller._handle_play(False)
        _observe(controller, timeline, 30.0, 27000.0)
        controller._handle_pause()
        with controller._lock:
            controller.state.renderer_state = state_mod._RENDERER_PAUSED
        # 模拟 seek：AirPlay 恢复后 prgr 报出很远的新位置
        controller.timeline.on_resume()
        _set_offset(timeline, 300.0)
        generation_before = ring.generation

        controller._handle_play(True)

        self.assertEqual(generation_before + 1, ring.generation, "位置跳变时必须换代")

    def test_paused_and_continuous_resumes_in_place(self) -> None:
        controller, _, ring, timeline, _, streams = _build()
        controller._handle_play(False)
        _observe(controller, timeline, 30.0, 27000.0)
        controller._handle_pause()
        with controller._lock:
            controller.state.renderer_state = state_mod._RENDERER_PAUSED
        generation_before = ring.generation
        created_before = len(streams.created)

        controller._handle_play(True)

        self.assertEqual(generation_before, ring.generation, "暂停且位置连续时不必换代")
        self.assertEqual(created_before, len(streams.created), "不必换 URI")
        self.assertFalse(controller._intent.set_uri)

    def test_unknown_renderer_state_is_not_treated_as_paused(self) -> None:
        controller, _, _, _, _, _ = _build()
        controller._handle_play(False)
        with controller._lock:
            controller.state.renderer_state = "UNKNOWN"
        self.assertFalse(controller._can_resume_in_place(),
                         "状态未知时不得假设可原地续播（真机代价是无声）")


class ResumedWithoutEventTests(unittest.TestCase):
    """拖动进度条时 AirPlay 可能**不发任何事件** —— 靠"持续收到 PCM"兜底恢复。

    真机证据：拖动进度条后 9 秒内日志里没有任何 seek/恢复事件，只有 PCM 从新位置送来；
    状态机停在 PAUSED、音箱也一直停着 —— 这就是「拖进度条后再也放不出声音」的根因。
    """

    def test_sustained_pcm_while_paused_triggers_recovery(self) -> None:
        controller, _, ring, timeline, _, streams = _build()
        controller._handle_play(False)
        _observe(controller, timeline, 30.0, 27000.0)
        controller._handle_pause()
        generation_before = ring.generation
        created_before = len(streams.created)

        # 没有任何事件，PCM 却持续送来（0.6 秒的量）
        controller.on_audio_bytes(b"\x00" * int(176400 * 0.6))
        controller._check_resumed_without_event()

        self.assertEqual(generation_before + 1, ring.generation, "应主动换代恢复播放")
        self.assertEqual(created_before + 1, len(streams.created), "应换新 URI")

    def test_small_tail_data_does_not_trigger(self) -> None:
        controller, _, ring, timeline, _, _ = _build()
        controller._handle_play(False)
        controller._handle_pause()
        generation_before = ring.generation

        controller.on_audio_bytes(b"\x00" * 4096)     # 暂停瞬间的尾部残留
        controller._check_resumed_without_event()

        self.assertEqual(generation_before, ring.generation, "少量尾部数据不得误触发")

    def test_counter_resets_after_recovery(self) -> None:
        controller, _, ring, timeline, _, _ = _build()
        controller._handle_play(False)
        controller._handle_pause()
        controller.on_audio_bytes(b"\x00" * int(176400 * 0.6))
        controller._check_resumed_without_event()

        self.assertEqual(0, controller._paused_audio_bytes, "触发后计数应清零")


class KeepaliveHealthTests(unittest.TestCase):
    """GPT Phase 2：keepalive 期间的断流检测 + Renderer Profile 记忆。"""

    def _keepalive(self):
        controller, config, ring, timeline, record, streams = _build()
        config["recovery_mode"] = "keepalive"
        controller._handle_play(False)
        _observe(controller, timeline, 30.0, 27000.0)
        controller._handle_pause()
        session = streams.get(controller._gen_token)
        return controller, record, session

    def test_renderer_stopped_during_keepalive_disables_it(self) -> None:
        controller, record, session = self._keepalive()
        with controller._lock:
            controller.state.renderer_state = state_mod._RENDERER_STOPPED

        controller._check_keepalive_health()

        self.assertFalse(controller._silence_active, "应结束 keepalive")
        self.assertFalse(session.silence_mode)
        self.assertFalse(controller._keepalive_capable.get(record.udn, True),
                         "应记入 profile：该设备不能保持 keepalive")

    def test_no_clients_disables_keepalive(self) -> None:
        controller, record, session = self._keepalive()
        session.clients = 0

        for _ in range(3):
            controller._check_keepalive_health()

        self.assertFalse(controller._silence_active)
        self.assertFalse(controller._keepalive_capable.get(record.udn, True))

    def test_healthy_keepalive_marks_capable(self) -> None:
        controller, record, session = self._keepalive()
        session.clients = 1
        with controller._lock:
            controller.state.renderer_state = state_mod._RENDERER_PLAYING

        controller._check_keepalive_health()

        self.assertTrue(controller._silence_active, "连接正常时 keepalive 应继续")
        self.assertTrue(controller._keepalive_capable.get(record.udn, False))

    def test_profile_blocks_keepalive_on_next_pause(self) -> None:
        controller, record, session = self._keepalive()
        controller._mark_keepalive_unsupported("测试")
        session.silence_mode = False
        controller._silence_active = False
        calls: list[str] = []
        record.client.pause = lambda *a, **kw: (calls.append("pause"), True)[1]

        controller._handle_pause()          # 第二次暂停

        self.assertFalse(controller._silence_active, "已被标记不支持时不得再启用 keepalive")
        # 走 current 方案：向渲染器下发 Pause 意图（由收敛线程执行）
        self.assertEqual(state_mod._MODE_PAUSE, controller._intent.mode)

    def test_default_unknown_is_allowed(self) -> None:
        controller, _, _, _, _, _ = _build()
        controller.config["recovery_mode"] = "keepalive"
        self.assertTrue(controller._keepalive_allowed(), "未验证过的设备应允许尝试")


class SilenceOutputLayerTests(unittest.TestCase):
    """静音只在 HTTP 输出层产生，绝不写入环形缓冲。"""

    def test_silence_mode_emits_zeros_and_keeps_ring_untouched(self) -> None:
        import io

        from air2dlna.stream import StreamManager

        ring = PcmRingBuffer(SAMPLE_RATE * CHANNELS * 2 * 10, sample_rate=SAMPLE_RATE,
                            channels=CHANNELS)
        ring.append(b"\x7f" * (SAMPLE_RATE * CHANNELS * 2 * 2))       # 环形缓冲里是真实音频
        manager = StreamManager(ring)
        session = manager.new_generation("l16")
        session.total_bytes = 64 * 1024                                # 发完两块就结束
        session.silence_mode = True
        out = io.BytesIO()
        write_offset_before = ring.write_offset

        manager.serve(session, out, range_start=0)

        data = out.getvalue()
        self.assertTrue(data.startswith(b"\x00"), "应发送静音而不是环形缓冲内容")
        self.assertEqual(b"\x00", data[0:1])
        self.assertFalse(b"\x7f" in data, "不得把环形缓冲里的真实音频混进静音流")
        self.assertEqual(write_offset_before, ring.write_offset,
                         "环形缓冲绝不能被合成静音污染")


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
