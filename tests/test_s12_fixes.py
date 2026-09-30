"""针对真机 1.0.23 反馈的三个缺陷的回归测试。

对应现象（用户真机反馈）：
  1. 暂停 / 恢复各有 5~6 秒延迟；
  2. 拖动进度条后**完全没有声音**。

根因（全部来自真机日志与代码核对，不是猜测）：

* **缺陷 A —— 换代后旧 token 只拿到 0 字节。**
  `new_generation()` 会把旧会话标记 `closed`，而 `serve()` 见到 `closed` 就 `break`。
  真机日志：gen=3 已开始后，渲染器仍在拉 token=...-2（`range_start=10138872`
  与 `44` 两次），结果 `本次连接 0.0s` —— 0 字节，即「拖完进度条没声音」。
* **缺陷 B —— 预滚动期间换代会让整轮收敛中断。**
  `do_play()` 调用 `ring.wait_for_data()` 等待预滚动；seek 会连续产生多代
  （`pdis` 一代 + `pbeg/prsm` 一代），等待被 `StaleGeneration` 打断并抛出
  `converge`，于是 `SetAVTransportURI`/`Play` 根本没发出去。
  真机日志：`ERROR [dlna_output] DLNA 收敛过程异常 ... StaleGeneration`。
* **缺陷 C —— Renderer Profile 选择结果被按 UDN 永久缓存。**
  真机日志：`Renderer Profile 选择: name='小爱音箱-2284' model='' -> generic`。
  SSDP 阶段只有名字、`model` 还是空串，`model='S12'` 要等设备描述抓回来才有；
  旧缓存键只有 UDN，于是 **generic 被缓存整个进程生命周期**，
  xiaomi_s12 Profile（及其暂停语义）永远不生效。
* **缺陷 D —— 对「暂停实为 Stop」的设备仍启用 keepalive。**
  S12 的 Profile 声明 `supports_pause=False`，但 keepalive 的策略是
  「不对渲染器发任何 UPnP 命令、只在输出层送静音」，于是音箱要先把已缓冲的
  真实 PCM 放完（约 5~6s）暂停才生效，恢复时又要把缓冲里的静音放完（约 5~6s）
  —— 正是现象 1。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "app", "server"))

from air2dlna.ringbuffer import PcmRingBuffer            # noqa: E402
from air2dlna.stream import StreamManager                 # noqa: E402
from air2dlna.renderer_profile import profile_for, select_profile  # noqa: E402

from test_virtual_player import (                         # noqa: E402
    BYTE_RATE, CHANNELS, SAMPLE_RATE, _Record, build_controller,
)


class _FakeWFile:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, chunk: bytes) -> int:
        self.data.extend(chunk)
        return len(chunk)

    def flush(self) -> None:
        pass


def _pcm(seconds: float) -> bytes:
    size = int(BYTE_RATE * seconds)
    return (bytes(range(256)) * (size // 256 + 1))[:size]


# --------------------------------------------------------------- 缺陷 A
class SupersededTokenTests(unittest.TestCase):
    """换代后旧 token 必须仍能拿到音频，而不是 0 字节。"""

    def _manager(self):
        ring = PcmRingBuffer(BYTE_RATE * 30, sample_rate=SAMPLE_RATE, channels=CHANNELS)
        return ring, StreamManager(ring, sample_rate=SAMPLE_RATE, channels=CHANNELS)

    def test_superseded_token_keeps_serving_current_generation(self):
        ring, manager = self._manager()
        ring.flush()
        ring.append(_pcm(2.0))                       # gen=1
        old = manager.new_generation("wav", duration_ms=None)
        old.continuous_output = True
        old.read_timeout_s = 0.2
        # 模拟换代：新会话产生，旧会话被标记 closed（这正是 new_generation 的行为）
        ring.flush()                                  # gen=2
        ring.append(_pcm(2.0))
        newest = manager.new_generation("wav", duration_ms=None)
        self.assertTrue(old.closed, "new_generation 应关闭旧会话（复现前提）")
        self.assertNotEqual(old.generation, ring.generation)

        out = _FakeWFile()
        done = threading.Event()

        def run():
            manager.serve(old, out, idle_timeout=1.0)
            done.set()

        threading.Thread(target=run, daemon=True).start()
        deadline = time.time() + 6.0
        while len(out.data) < 44 + 4096 and time.time() < deadline:
            time.sleep(0.02)

        self.assertGreater(
            len(out.data), 44 + 4096,
            "被换代取代的旧 token 只拿到 %d 字节（旧实现为 44 字节 = 只有 WAV 头，"
            "真机表现为拖完进度条后没声音）" % len(out.data))
        self.assertEqual(out.data[:4], b"RIFF")
        self.assertTrue(newest.token != old.token)
        old.closed = True
        done.wait(3.0)

    def test_inflight_connection_survives_generation_rollover(self):
        """播放进行中被真实换代打断：连接必须继续输出最新一代，而不是结束。

        复现真机日志里 ``token=...-2`` 在 gen=3 已开始后仍被拉取、
        结果是 ``本次连接 0.0s``（0 字节）的那一幕。
        """
        ring, manager = self._manager()
        ring.flush()
        ring.append(_pcm(2.0))
        session = manager.new_generation("wav", duration_ms=None)
        session.continuous_output = True
        session.read_timeout_s = 0.2

        out = _FakeWFile()
        done = threading.Event()

        def run():
            manager.serve(session, out, idle_timeout=15.0)
            done.set()

        threading.Thread(target=run, daemon=True).start()
        deadline = time.time() + 6.0
        while len(out.data) < 8192 and time.time() < deadline:
            time.sleep(0.02)
        before = len(out.data)

        # 真实换代：先 flush 再 new_generation（new_generation 会顺手关闭旧会话），
        # 与 virtual_player._begin_new_generation 的顺序一致。
        ring.flush()
        manager.new_generation("wav", duration_ms=None)
        ring.append(_pcm(2.0))
        time.sleep(0.6)

        self.assertFalse(done.is_set(),
                         "换代会关闭旧会话，但渲染器仍在拉取，连接不应结束（否则拿到 0 字节）")
        self.assertGreater(len(out.data), before,
                           "换代后应继续输出最新一代的数据，而不是 0 字节断流")
        session.closed = True
        done.wait(4.0)


# --------------------------------------------------------------- 缺陷 B
class PrerollStaleGenerationTests(unittest.TestCase):
    """预滚动期间换代不得中断收敛（否则 SetURI/Play 发不出去）。"""

    def test_do_play_survives_stale_generation_during_preroll(self):
        controller, config, ring, _timeline, record, streams = build_controller(model="S12")
        controller._handle_play(False)
        token = controller._gen_token
        session = streams.get(token)
        self.assertIsNotNone(session)

        from air2dlna.ringbuffer import StaleGeneration
        original = ring.wait_for_data
        calls = {"n": 0}

        def raiser(offset, generation, needed, timeout):
            calls["n"] += 1
            raise StaleGeneration("等待预滚动期间缓冲换代（测试模拟真实 seek 的连续换代）")

        ring.wait_for_data = raiser
        try:
            # 旧实现在这里会把 StaleGeneration 抛出 converge —— 那就是真机上
            # 「ERROR DLNA 收敛过程异常」以及「拖进度条后无声」的直接原因。
            controller.output.do_play(record, record.client, token,
                                      set_uri=True, play=True)
        except StaleGeneration:  # pragma: no cover
            self.fail("do_play 不应把预滚动期间的 StaleGeneration 抛出去（会中断收敛）")
        finally:
            ring.wait_for_data = original

        self.assertEqual(calls["n"], 1, "应只尝试一次预滚动等待")


# --------------------------------------------------------------- 缺陷 C
class ProfileSelectionTests(unittest.TestCase):
    """真机身份必须能选中 xiaomi_s12。"""

    def test_real_device_identity_selects_s12(self):
        record = _Record(model="S12", udn="uuid:64f2215e-dc4f-4680-9b2d-e8699f4c43ad")
        profile = select_profile(record)
        self.assertEqual(profile.name, "xiaomi_s12",
                         "真机 model='S12'/manufacturer='Mi, Inc.' 必须命中 xiaomi_s12")
        self.assertFalse(profile.supports_pause)
        self.assertFalse(profile.pause_keeps_http)
        self.assertTrue(profile.resume_requires_reannounce)

    def test_model_unknown_falls_back_to_generic(self):
        profile = profile_for(model="", name="小爱音箱-2284", manufacturer="")
        self.assertEqual(profile.name, "generic",
                         "身份不足时必须保守回退 generic（不猜）")

    def test_profile_cache_invalidated_when_identity_improves(self):
        """缺陷 C 的核心回归：model 迟到时，缓存必须失效并改用 xiaomi_s12。"""
        controller, _config, _ring, _timeline, record, _streams = build_controller(model="")
        # 第一次：SSDP 阶段，只有名字，model 还是空串（真机日志的真实顺序）
        self.assertEqual(controller._profile_for().name, "generic")
        # 随后设备描述抓回来，model 变为 S12
        record.model = "S12"
        record.manufacturer = "Mi, Inc."
        self.assertEqual(
            controller._profile_for().name, "xiaomi_s12",
            "model 迟到后 Profile 缓存必须失效，否则 xiaomi_s12 永远不生效"
            "（真机日志：model='' -> generic 之后再无重算）")


# --------------------------------------------------------------- 缺陷 D
class KeepaliveProfileVetoTests(unittest.TestCase):
    """对 supports_pause=False 的设备必须否决 keepalive。"""

    def test_keepalive_vetoed_for_s12_profile(self):
        controller, config, _ring, _timeline, _record, _streams = build_controller(model="S12")
        config["recovery_mode"] = "keepalive"
        self.assertEqual(controller._recovery_mode(), "keepalive")
        self.assertFalse(
            controller._keepalive_allowed(),
            "S12 Profile 声明 supports_pause=False，keepalive 应被否决"
            "（否则暂停/恢复各延迟 5~6 秒）")

    def test_keepalive_allowed_for_generic_profile(self):
        controller, config, _ring, _timeline, _record, _streams = build_controller(model="")
        config["recovery_mode"] = "keepalive"
        self.assertTrue(controller._keepalive_allowed(),
                        "generic（保守假设 supports_pause=True）不应被否决")


if __name__ == "__main__":
    unittest.main(verbosity=2)
