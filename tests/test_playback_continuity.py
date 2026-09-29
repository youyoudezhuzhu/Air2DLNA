"""播放连续性回归测试 —— 对应真机两个问题（1.0.3 修复）。

问题 1「播放一小段，声音突然变小又变大（拼接痕迹）」
    小爱音箱开始播放时有淡入过渡。1.0.2 修掉基准偏移后，漂移从 71 秒降到
    2.9~4.3 秒，但这是**渲染器固有延迟**（音箱内部缓冲 + 淡入），仍超过 1500ms
    阈值 → 每 10-12 秒重建一次会话 → 每次重建音箱重新淡入，听感就是拼接痕迹。
    修复：把「稳定的连续漂移」判定为固定延迟并**自动补偿**，补偿得了的绝不重建。

问题 2「iPhone 拖动进度条后声音直接停止」
    日志显示 seek/恢复之后渲染器不再来拉流，20 秒后我们断开连接、声音停止。
    修复：轮询里检查「声称在播放但没有客户端在拉流」，超过 STREAM_STALL_SECONDS
    就重建会话把它拉回来。
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


class _Record:
    def __init__(self, client=None) -> None:
        self.udn = "uuid:fake"
        self.name = "小爱音箱-测试"
        self.ip = "192.168.1.60"
        self.client = client


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
        return None


def _build():
    config = _Cfg({
        "drift_threshold_ms": 1500,
        "preroll_seconds": 2.0,
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
    controller._session_started_at = time.monotonic() - 60   # 已过宽限期
    controller.timeline.on_play_begin()
    return controller, config, ring, timeline, record


def _set_offset(timeline: AudioTimeline, seconds: float) -> None:
    samples = int(seconds * SAMPLE_RATE)
    timeline.on_prgr(f"0/{samples}/10000000 44100")
    timeline.on_phbt(f"{samples}/{monotonic_raw_ns()}")


class FixedLatencyAbsorptionTests(unittest.TestCase):
    """问题 1：渲染器固有延迟应当被补偿，而不是反复重建（每次重建都会重新淡入）。"""

    def test_stable_offset_is_absorbed_not_rebuilt(self) -> None:
        controller, config, ring, timeline, record = _build()
        # 复现真机数值：曲目位置 11.9 秒、渲染器只报到 9.0 秒 → 固定偏差 ~2.9 秒。
        # 注意 GetPositionInfo 的 rel_time 单位是**毫秒**。
        _set_offset(timeline, 11.9)
        timeline.update_renderer_position(9000.0)
        generation_before = ring.generation

        for i in range(controller.DRIFT_STABLE_SAMPLES):
            # 轮询间隔 3 秒：曲目位置与渲染器位置同步前进，偏差保持在 2.9~3.2 秒
            # （真机实测该偏差在 2.9~4.3 秒之间小幅抖动，这里模拟 300ms 级抖动）
            _set_offset(timeline, 11.9 + i * 3.0)
            timeline.update_renderer_position(9000.0 + i * 3000 - (i % 2) * 300)
            controller._check_drift(record)

        self.assertEqual(0, controller._drift_rebuilds, "固定延迟不应触发会话重建")
        self.assertEqual(generation_before, ring.generation, "不应换代")
        self.assertGreater(timeline.renderer_latency_ms, 1000.0, "延迟补偿应被写入时间线")
        # 补偿后漂移必须收敛
        self.assertLess(abs(timeline.drift_ms()), 1500, "补偿后漂移应收敛")

    def test_latency_persisted_to_config(self) -> None:
        controller, config, _ring, timeline, record = _build()
        for i in range(controller.DRIFT_STABLE_SAMPLES):
            # 曲目位置与渲染器位置同步前进，保持固定偏差（否则样本间符号会翻转，
            # 达不到判定所需的连续样本数）
            _set_offset(timeline, 11.9 + i * 3.0)
            timeline.update_renderer_position(9000.0 + i * 3000)
            controller._check_drift(record)
        self.assertIn("av_offset_ms", getattr(config, "update_dict", {}),
                      "学到的固定延迟应写回配置，下次播放无需重新学习")

    def test_drift_samples_are_not_polluted_across_rebuilds(self) -> None:
        """换代会改变基准偏移，旧样本必须清空。

        真机 1.0.3 日志：换代前的样本是 23 秒级、换代后是 3~4 秒级，混在同一个
        样本窗口里让相邻差值恒为十几秒 → 固定延迟永远识别不出来 →
        「重建间隔被拉长，但周期性声音变小依旧存在」。
        """
        controller, _config, ring, timeline, record = _build()

        # 第一次：巨大的偏差（23 秒）→ 超补偿上限 → 必须重建
        _set_offset(timeline, 48.0)
        timeline.update_renderer_position(25000.0)
        for _ in range(controller.DRIFT_STABLE_SAMPLES):
            controller._check_drift(record)
        self.assertEqual(1, controller._drift_rebuilds, "超上限偏差应触发重建")
        self.assertEqual([], list(controller._drift_history),
                         "换代后不得残留上一代的样本（否则固定延迟永远识别不出来）")

        # 第二次：换代后渲染器稳定落后 3 秒 → 应被补偿，而不是再来一次重建。
        # 建模方式：渲染器实际从曲目 45 秒处开始出声（比真实位置 48 秒落后 3 秒），
        # 之后 rel_time 正常增长 → 恒定 3 秒偏差。
        timeline.begin_generation(ring.generation, 45000.0)
        for i in range(controller.DRIFT_STABLE_SAMPLES):
            _set_offset(timeline, 48.0 + i * 3.0)
            timeline.update_renderer_position(i * 3000.0)
            controller._check_drift(record)
        self.assertEqual(1, controller._drift_rebuilds,
                         "固定延迟应被补偿，不应再次重建会话")
        self.assertGreater(timeline.renderer_latency_ms, 2000.0, "应补偿约 3 秒的固定延迟")

    def test_growing_drift_still_rebuilds(self) -> None:
        """真正的漂移（持续变大）仍要重建，不能被当成固定延迟吞掉。"""
        controller, _config, ring, timeline, record = _build()
        generation_before = ring.generation
        for i in range(controller.DRIFT_STABLE_SAMPLES + 1):
            _set_offset(timeline, 10.0 + i * 5.0)         # 每轮漂移增加 5 秒 → 持续增长
            timeline.update_renderer_position(0.0)        # 渲染器位置不动（0 ms）
            controller._check_drift(record)

        self.assertGreaterEqual(controller._drift_rebuilds, 1, "持续变大的漂移必须重建")
        self.assertGreater(ring.generation, generation_before, "重建必须先换代")

    def test_absurd_offset_not_absorbed(self) -> None:
        """超出补偿上限的偏差按真实漂移处理。"""
        controller, _config, ring, timeline, record = _build()
        generation_before = ring.generation
        for i in range(controller.DRIFT_STABLE_SAMPLES + 2):
            _set_offset(timeline, 60.0)
            timeline.update_renderer_position(0.0)
            controller._check_drift(record)
        self.assertGreater(ring.generation, generation_before)


class RendererStallRecoveryTests(unittest.TestCase):
    """问题 2：渲染器不再拉流（如 iPhone 拖进度条后）应被兜底救回。"""

    def test_stalled_renderer_triggers_rebuild(self) -> None:
        controller, _config, ring, timeline, record = _build()
        session = controller.streams.new_generation("wav", None)
        session.clients = 0
        session.created_at = time.monotonic() - 30      # 30 秒前建立
        session.last_activity = 0.0                      # 从未写过数据
        controller._renderer_token = session.token
        with controller._lock:
            controller.state.state = state_mod.PLAYING
        generation_before = ring.generation

        controller._check_renderer_stream()

        self.assertGreater(ring.generation, generation_before, "应换代重建")
        self.assertGreater(len(controller.streams.created), 1, "应创建新的流会话")

    def test_active_stream_is_not_rebuilt(self) -> None:
        controller, _config, ring, _timeline, _record = _build()
        session = controller.streams.new_generation("wav", None)
        session.clients = 1
        session.last_activity = time.monotonic()          # 正在拉流
        session.created_at = time.monotonic() - 30
        controller._renderer_token = session.token
        with controller._lock:
            controller.state.state = state_mod.PLAYING
        generation_before = ring.generation

        controller._check_renderer_stream()

        self.assertEqual(generation_before, ring.generation, "正常拉流时不该重建")

    def test_recent_session_within_stall_window_is_left_alone(self) -> None:
        controller, _config, ring, _timeline, _record = _build()
        session = controller.streams.new_generation("wav", None)
        session.clients = 0
        session.created_at = time.monotonic()            # 刚建立，渲染器还在准备
        controller._renderer_token = session.token
        with controller._lock:
            controller.state.state = state_mod.PLAYING
        generation_before = ring.generation

        controller._check_renderer_stream()

        self.assertEqual(generation_before, ring.generation, "不应立刻重建（给渲染器准备时间）")


if __name__ == "__main__":
    unittest.main()
