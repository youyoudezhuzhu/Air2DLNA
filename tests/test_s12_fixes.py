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

from air2dlna import state as state_mod                    # noqa: E402

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

        # 预滚动等待只存在于**非连续输出**路径；显式关闭以覆盖该分支
        session.continuous_output = False
        ring.wait_for_data = raiser
        try:
            # 旧实现在这里会把 StaleGeneration 抛出 converge —— 那就是真机上
            # 「ERROR [dlna_output] DLNA 收敛过程异常」的直接原因。
            controller.output.do_play(record, record.client, token,
                                      set_uri=True, play=True)
        except StaleGeneration:  # pragma: no cover
            self.fail("do_play 不应把预滚动期间的 StaleGeneration 抛出去（会中断收敛）")
        finally:
            ring.wait_for_data = original

        self.assertEqual(calls["n"], 1, "非连续输出路径应只尝试一次预滚动等待")

    def test_continuous_output_does_not_block_on_preroll(self):
        """连续输出下必须**立即宣告 URI**，绝不等待预滚动。

        真机实测（1.0.25）：换代 → SetAVTransportURI 的延迟在非 seek 重建时为 1~2 秒，
        而两次 seek 都是**整整 8.0 秒**（预滚动超时）——因为 seek 之后 iPhone 要 10~20 秒
        才重新送出音频，等待把这段时间原封不动加在了 SetAVTransportURI **之前**，
        期间渲染器 STOPPED、用户完全无声。输出层本来就会用静音填充空窗，
        所以连续输出下必须跳过等待。
        """
        controller, config, ring, _timeline, record, streams = build_controller(model="S12")
        controller._handle_play(False)
        token = controller._gen_token
        session = streams.get(token)
        session.continuous_output = True

        calls = {"n": 0}
        original = ring.wait_for_data

        def raiser(offset, generation, needed, timeout):
            calls["n"] += 1
            raise AssertionError("连续输出下不得等待预滚动（真机会白白阻塞 8 秒）")

        ring.wait_for_data = raiser
        try:
            controller.output.do_play(record, record.client, token,
                                      set_uri=True, play=True)
        finally:
            ring.wait_for_data = original

        self.assertEqual(calls["n"], 0,
                         "连续输出下 do_play 不得调用 wait_for_data（否则 seek 时阻塞 8 秒）")


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


# --------------------------------------------------------------- 拖进度条后无声
class SeekReannounceTests(unittest.TestCase):
    """拖进度条后 iPhone 会 pend → 重建会话 → pbeg/prsm，这段空窗里渲染器必然已 STOPPED。

    真机日志（1.0.24，每次拖进度条都会命中）::

        21:26:33 pbeg：曲目位置连续（变化 0 ms），沿用当前 DLNA 会话
        21:26:33 pbeg：沿用当前 DLNA 会话 gen=4（不重建、不重设 URI）
        21:26:34 连续输出静音超时：进入 RECOVERING（渲染器=STOPPED 拉流连接=0）

    渲染器已 STOPPED、拉流连接=0，却「沿用会话」既不 SetAVTransportURI 也不 Play ——
    没有任何人会去播放，音箱永远无声。
    """

    def test_renderer_stopped_must_not_reuse(self):
        controller, _c, _ring, _t, _r, streams = build_controller(model="")
        controller._handle_play(False)
        controller.state.renderer_state = state_mod._RENDERER_STOPPED
        streams.get(controller._gen_token).clients = 3
        self.assertFalse(controller._can_reuse_generation(),
                         "渲染器已 STOPPED 时不得沿用会话（否则无人播放）")

    def test_no_http_client_must_not_reuse(self):
        controller, _c, _ring, _t, _r, streams = build_controller(model="")
        controller._handle_play(False)
        controller.state.renderer_state = state_mod._RENDERER_PLAYING
        streams.get(controller._gen_token).clients = 0
        self.assertFalse(controller._can_reuse_generation(),
                         "没有任何客户端在拉流时不得沿用会话")

    def test_positive_control_reuse_still_allowed_when_renderer_can_continue(self):
        """反向对照：渲染器在播且有客户端时，沿用仍然必须被允许。

        防止把 _can_reuse_generation 改成「永远返回 False」这种假修复
        （那会让每次 pbeg 都换代，音箱重复缓冲 → 真机卡顿）。
        """
        controller, _c, _ring, _t, _r, streams = build_controller(model="")
        controller._handle_play(False)
        controller.state.renderer_state = state_mod._RENDERER_PLAYING
        streams.get(controller._gen_token).clients = 1
        self.assertTrue(controller._can_reuse_generation(),
                        "渲染器可继续播放且位置连续时应允许沿用会话")

    def test_s12_profile_never_reuses_because_resume_needs_reannounce(self):
        """S12 的 resume_requires_reannounce=True 此前是**死字段**，无人使用。"""
        controller, _c, ring, _t, _r, streams = build_controller(model="S12")
        controller._handle_play(False)
        controller.state.renderer_state = state_mod._RENDERER_PLAYING
        streams.get(controller._gen_token).clients = 5
        self.assertFalse(controller._can_reuse_generation(),
                         "S12 恢复必须重新宣告（supports_pause=False → Pause 实为 Stop）")
        generation_before = ring.generation
        controller._handle_play(False)               # 紧随的 pbeg
        self.assertGreater(ring.generation, generation_before,
                           "S12 的 pbeg 必须换代并重新宣告，否则拖进度条后无声")


# --------------------------------------------------------------- SEEK 实验（1.0.27）
class SeekHoldExperimentTests(unittest.TestCase):
    """SEEK 实验：seek 时保持 DLNA 会话与 HTTP 连接，不 Stop、不 SetURI、不换 URI。

    真机证据：seek 后 AirPlay 有 10~20 秒没有 PCM，而旧实现会主动 Stop 掉渲染器
    正在用的 HTTP 连接并重建播放生命周期，渲染器建好即空转，随后自行 STOPPED
    （日志里 PCM 刚恢复就 `渲染器=STOPPED 拉流连接=0`）。
    """

    def _playing(self, model="S12"):
        controller, _cfg, ring, _tl, record, streams = build_controller(model=model)
        controller._handle_play(False)
        return controller, ring, record, streams

    def test_seek_does_not_send_stop_even_for_s12_profile(self):
        """S12 档案声明 supports_pause=False（暂停→Stop），但 seek 期间绝不能 Stop。"""
        controller, _ring, record, _streams = self._playing()
        record.client.calls.clear()
        controller._handle_flush("999")          # seek
        controller._handle_pause()               # seek 过程中的暂停事件
        self.assertNotIn("stop", record.client.calls,
                         "SEEK 实验：seek 保持窗口内不得向渲染器发送 Stop"
                         "（会拆掉它正在用的 HTTP 连接）")
        self.assertNotIn("set_uri", record.client.calls,
                         "SEEK 实验：seek 保持窗口内不得 SetAVTransportURI")

    def test_seek_hold_ignores_pend(self):
        controller, _ring, _record, _streams = self._playing()
        controller._handle_flush("999")
        controller._handle_play_stream_end("播放流结束")   # pend 迟到
        self.assertFalse(controller._awaiting_new_stream,
                         "SEEK 实验：保持窗口内收到 pend 不得进入过渡态（会被误判成播放结束）")
        self.assertNotEqual(state_mod.STOPPED, controller.state.state)
        self.assertTrue(controller._in_seek_hold())

    def test_seek_hold_ends_when_pcm_arrives(self):
        controller, _ring, _record, streams = self._playing()
        controller._handle_flush("999")
        self.assertTrue(controller._in_seek_hold())
        controller.on_audio_bytes(b"\x00" * 4096)          # 新 PCM 到达（音频线程）
        self.assertTrue(controller._seek_pcm_seen, "音频线程应记录 T6")
        controller._sample_seek_hold()                      # 轮询线程收尾
        self.assertFalse(controller._in_seek_hold(), "PCM 到达后应结束保持窗口")

    def test_seek_probe_records_t0_and_t1(self):
        """打点必须能回答「T6 之后是渲染器先停（T8）还是我们先断（T7）」。"""
        controller, _ring, _record, _streams = self._playing()
        controller._handle_flush("999")
        timeline = controller._seek_timeline
        self.assertIsNotNone(timeline)
        controller._handle_pause()
        controller.on_audio_bytes(b"\x00" * 1024)
        names = [m[0] for m in timeline.marks]
        self.assertIn("T0_seek_detected", names)
        self.assertIn("T1_no_stop_sent", names)
        self.assertIn("T6_first_real_pcm", names)
        controller._finish_seek_hold("测试结束")


# --------------------------------------------------------------- 音频入口接线
class AudioIngestWiringTests(unittest.TestCase):
    """回归：音频必须**经 VirtualPlayer** 进入环形缓冲。

    真机 bug：``bridge.py`` 把 ``AudioPipeReader`` 的 sink 直接绑成裸 ring
    （``AudioPipeReader(self.audio_fifo, self._ring)``），而 ``VirtualPlayer.on_audio_bytes``
    在整个代码库里**只有定义、没有任何调用点**（只有测试调用过）。于是它承载的两条兜底
    在生产环境**从未执行**，而这正是「拖进度条后无声」长期修不好的原因之一：

    * 暂停期间持续收到 PCM ⇒ 判定 AirPlay 其实已恢复
      （真机实测拖动进度条时 AirPlay 可能**完全不发** pbeg/pres/pfls 任何事件，
       只靠事件永远醒不过来）；
    * 仍在推送 PCM ⇒ 不把 ``pend`` 当成播放流真正结束。

    对照：同一处的元数据读取器是**正确**绑到 controller 的
    （``MetadataPipeReader(self.metadata_fifo, self._controller.on_metadata_item)``），
    足以说明音频这处是遗漏而非设计。
    """

    def test_reader_sink_protocol_reaches_ring(self):
        """AudioPipeReader 调用的是 sink.append(chunk)。"""
        controller, _c, ring, _t, _r, _s = build_controller(model="S12")
        before = ring.write_offset
        controller.append(b"\x01" * 1024)
        self.assertEqual(ring.write_offset, before + 1024, "数据应进入环形缓冲")

    def test_reader_sink_byte_rate_exposed(self):
        controller, _c, _ring, _t, _r, _s = build_controller(model="S12")
        self.assertEqual(controller.byte_rate, 176400,
                         "读取器需要 sink.byte_rate 做丢弃统计")

    def test_paused_audio_fallback_runs_through_controller(self):
        """核心：暂停期间送进来的 PCM 必须被 VirtualPlayer 计数。"""
        controller, _c, _ring, _t, _r, _s = build_controller(model="S12")
        controller.state.state = state_mod.PAUSED
        controller.append(b"\x00" * 4096)
        self.assertGreater(
            controller._player._paused_audio_bytes, 0,
            "经 controller 送出的 PCM 必须触发 VirtualPlayer 的暂停恢复兜底；"
            "若为 0 说明音频又绕过了 VirtualPlayer（sink 直接绑 ring）")

    def test_pcm_during_pend_transition_cancels_it(self):
        """pend 过渡态中仍有 PCM ⇒ 说明流并未真正结束，应取消过渡。"""
        controller, _c, _ring, _t, _r, _s = build_controller(model="S12")
        controller._handle_play(False)
        controller._handle_play_stream_end("播放流结束")
        self.assertTrue(controller._awaiting_new_stream)
        controller.append(b"\x00" * 4096)
        self.assertFalse(controller._awaiting_new_stream,
                         "仍有 PCM 时必须取消 pend 过渡（该兜底此前从未执行）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
