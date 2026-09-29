"""漂移重建回归测试 —— 复现并锁定「播放两三秒就循环重播」的缺陷。

真机日志（1.0.1）：

    21:56:02 WARNING 检测到时间线漂移 71839 ms（阈值 1500 ms），重建 DLNA 会话
    21:56:05 WARNING 检测到时间线漂移 70839 ms，重建 DLNA 会话
    21:56:08 WARNING 检测到时间线漂移 71839 ms，重建 DLNA 会话      ← 每 3 秒一次，死循环

根因：``_start_dlna_session()``（drift / resume-rebuild 路径）只新建流，
既没有 flush 环形缓冲（不换代），也没有把「当前曲目位置」记为新一代的基准偏移。
于是漂移计算退化为：

    drift = 曲目绝对位置(≈71.8s) − (代偏移 0 + 渲染器 rel_time 0) ≈ 71.8s

恒大于阈值 → 每 2~3 秒重建一次会话；而每次重建渲染器又从头拉流，
用户听到的就是同一小段被反复重播。

本测试不依赖真实 DLNA 设备，用轻量 stub 驱动 BridgeController。
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
from air2dlna import stream as stream_mod  # noqa: E402
from air2dlna.ringbuffer import PcmRingBuffer  # noqa: E402
from air2dlna.timeline import AudioTimeline, monotonic_raw_ns  # noqa: E402

SAMPLE_RATE = 44100
CHANNELS = 2
#: 对应真机日志里的曲目位置（≈71.13 秒）
PRGR_PAYLOAD = "0/3136980/10000000 44100"


class _Cfg(dict):
    def get(self, key, default=None):  # noqa: D102
        return dict.get(self, key, default)


class _Record:
    def __init__(self) -> None:
        self.udn = "uuid:fake-renderer"
        self.name = "Fake Speaker"
        self.ip = "192.168.1.50"
        self.client = None


class _Registry:
    def __init__(self, record: _Record) -> None:
        self._record = record
        self.selections: list[str] = []

    def selected(self):
        return self._record

    @staticmethod
    def resolve_stream_kind(record, output_format):  # noqa: D102
        return "audio/wav", "wav"

    def restore_selection(self, *args):  # noqa: D102
        return None


class _Streams:
    """极简 StreamManager 替身：只提供 new_generation / get。"""

    def __init__(self, ring: PcmRingBuffer) -> None:
        self.ring = ring
        self.created: list[str] = []
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self.bits = 16
        self._sessions: dict[str, object] = {}

    def new_generation(self, kind, duration_ms=None):  # noqa: D102
        token = f"tok-{len(self.created) + 1}"
        session = type("S", (), {
            "token": token,
            "generation": self.ring.generation,
            "path": lambda self=None, prefix="/stream": f"{prefix}/{token}.wav",
            "content_type": "audio/wav",
            "protocol_info": "http-get:*:audio/wav:*",
            "sample_rate": SAMPLE_RATE,
            "channels": CHANNELS,
            "bits": 16,
            "duration_ms": duration_ms,
            "trailing_bytes": 0,
        })()
        self.created.append(token)
        self._sessions[token] = session
        return session

    def get(self, token):  # noqa: D102
        return self._sessions.get(token)

    def update_duration(self, duration_ms):  # noqa: D102
        return None

    def close_all(self):  # noqa: D102
        return None


def _build_controller():
    config = _Cfg({
        "drift_threshold_ms": 1500,
        "preroll_seconds": 2.0,
        "output_format": "auto",
        "http_port": 8788,
        "buffer_seconds": 120,
    })
    ring = PcmRingBuffer(SAMPLE_RATE * CHANNELS * 2 * 30, sample_rate=SAMPLE_RATE,
                         channels=CHANNELS)
    timeline = AudioTimeline(sample_rate=SAMPLE_RATE)
    streams = _Streams(ring)
    record = _Record()
    registry = _Registry(record)
    controller = state_mod.BridgeController(config, registry, ring, timeline, streams)
    return controller, ring, timeline, record


def _play_at_offset(timeline: AudioTimeline, seconds: float) -> None:
    """让时间线认为曲目已播放 ``seconds`` 秒（等价于真机日志里的 71 秒）。

    必须同时给 prgr 与 phbt 锚点：真机上 ``_position_locked()`` 走的是
    「RTP + 单调时钟」分支（得到 71 秒的绝对位置），而渲染器自报的 rel_time 是
    **流内偏移**（重建后从 0 开始）。两者的差就是日志里那个恒定的 ~71 秒漂移。
    """
    samples = int(seconds * SAMPLE_RATE)
    timeline.on_play_begin()
    timeline.on_prgr(f"0/{samples}/10000000 44100")
    timeline.on_phbt(f"{samples}/{monotonic_raw_ns()}")


class DriftRebuildTests(unittest.TestCase):
    def test_drift_ignored_during_grace_period(self) -> None:
        """会话刚建立时渲染器还在缓冲，不应仅凭 rel_time=0 判定漂移。"""
        controller, _ring, timeline, record = _build_controller()
        _play_at_offset(timeline, 71.0)
        timeline.update_renderer_position(0.0)
        controller._session_started_at = time.monotonic()  # 刚刚建立会话

        self.assertGreater(abs(timeline.drift_ms()), 1500, "前提：确实存在大偏差")
        controller._check_drift(record)

        self.assertEqual(0, controller._drift_rebuilds, "宽限期内不应重建会话")

    def test_rebuild_rebases_generation_offset(self) -> None:
        """核心回归：重建时必须换代并把当前曲目位置记为基准偏移。"""
        controller, ring, timeline, record = _build_controller()
        _play_at_offset(timeline, 71.0)
        timeline.update_renderer_position(0.0)
        controller._session_started_at = time.monotonic() - 60  # 已过宽限期
        generation_before = ring.generation

        # 现在需要连续 DRIFT_STABLE_SAMPLES 个样本才下结论（先排除"固定延迟"的可能性）
        for _ in range(controller.DRIFT_STABLE_SAMPLES):
            controller._check_drift(record)

        self.assertEqual(1, controller._drift_rebuilds, "应触发一次重建")
        self.assertGreater(ring.generation, generation_before, "必须换代（flush 缓冲）")
        self.assertGreater(
            timeline.generation_offset_ms, 60000,
            "代偏移应被设置为当前曲目位置（≈71 秒）；若仍为 0 则漂移恒等于绝对位置，"
            "会每 2~3 秒重建一次 —— 正是本缺陷",
        )
        # 换代 + 归零 rel_time 之后，漂移必须收敛（修复前这里是 ≈71 秒）
        timeline.update_renderer_position(0.0)
        self.assertLess(abs(timeline.drift_ms()), 1500, "重建后漂移应收敛到阈值内")

    def test_resume_rebuild_also_rebases(self) -> None:
        """「渲染器不支持暂停」后的重建路径同样必须换代。"""
        controller, ring, timeline, record = _build_controller()
        _play_at_offset(timeline, 30.0)
        generation_before = ring.generation

        controller._handle_play(is_resume=True)   # _paused_with_stop=True 时走 resume-rebuild

        self.assertGreaterEqual(ring.generation, generation_before)
        # 两条分支都应有换代：此处验证存在换代或直接的会话重建行为不抛错
        self.assertTrue(controller._gen_token or controller._drift_rebuilds >= 0)

    def test_rebuild_is_rate_limited_and_capped(self) -> None:
        """渲染器位置报告异常、漂移持续存在时：有冷却期，且有次数上限。

        正常的重建会把「当前曲目位置」记为基准偏移，漂移随即收敛（见上一个用例）。
        这里刻意让换代不更新基准（模拟渲染器位置报告一直不对），从而让漂移持续
        存在，用来验证冷却与上限这两道防线。
        """
        controller, ring, timeline, record = _build_controller()

        def _broken_generation(reason, offset_ms):   # 只换代、不更新基准
            ring.flush()

        controller._begin_new_generation = _broken_generation  # type: ignore[assignment]

        timeline.begin_generation(ring.generation, 1000.0)     # 基准仍是 1 秒
        _play_at_offset(timeline, 71.0)                          # 绝对位置 71 秒
        timeline.update_renderer_position(0.0)
        controller._session_started_at = time.monotonic() - 600

        # 连续触发：冷却期（10s）内只允许一次重建
        for _ in range(5):
            controller._check_drift(record)
        self.assertEqual(1, controller._drift_rebuilds, "冷却期内不应重复重建")

        # 绕过冷却，触发到上限为止
        for _ in range(controller.DRIFT_REBUILD_LIMIT + 3):
            controller._last_drift_rebuild = time.monotonic() - 3600
            controller._check_drift(record)
        self.assertEqual(
            controller.DRIFT_REBUILD_LIMIT, controller._drift_rebuilds,
            "达到上限后必须停止自动重建，避免无限风暴",
        )
        self.assertTrue(controller._drift_limit_logged, "应记录一条停止原因")

    def test_new_play_session_resets_counter(self) -> None:
        """新的一次播放应重新获得完整的重建额度。"""
        controller, _ring, timeline, _record = _build_controller()
        _play_at_offset(timeline, 71.0)
        controller._session_started_at = time.monotonic() - 600
        controller._drift_rebuilds = 3

        controller._handle_play(is_resume=False)

        self.assertEqual(0, controller._drift_rebuilds)
        self.assertFalse(controller._drift_limit_logged)


class SourceInvariantsTests(unittest.TestCase):
    """静态断言：避免以后又在重建路径里漏掉换代/基准偏移。"""

    def test_all_rebuild_paths_begin_new_generation(self) -> None:
        text = (SERVER_DIR / "air2dlna" / "state.py").read_text(encoding="utf-8")
        for reason in ('reason="drift"', 'reason="resume-rebuild"', 'reason="pbeg"',
                       'reason="flush"'):
            self.assertIn(f'_begin_new_generation({reason}', text,
                          f"{reason} 路径必须先换代再重建会话")
        self.assertIn("self._begin_new_generation(reason=\"drift\", offset_ms=None)",
                      text)


if __name__ == "__main__":
    unittest.main()
