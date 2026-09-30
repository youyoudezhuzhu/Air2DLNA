"""回归测试：同一 token 被多次拉取时，后续连接必须仍能拿到音频。

真机日志（/vol1/@appdata/air2dlna/bridge.log，1.0.20）显示的故障链：

  一次暂停恢复里，渲染器对**同一个 token** 连续发起了 5 次以上 GET
  （range_start=0 / 44 / 1030600 反复出现）。历史上流控用的是**会话级**
  session.bytes_served 计数，这些连接会把它叠加推过 total_bytes，于是之后的
  每个连接都在进入循环后立刻 break，并只补 `total_bytes - bytes_served` = 0
  字节的静音 —— 渲染器拿到空 body，表现就是「暂停 / 拖进度条之后音响不响」。

本测试锁定两件事：
  1. 多次连接必须各自都能拿到音频（按连接独立计数）。
  2. Range 探测（bytes=44-、bytes=1030600-）不得被当成环形缓冲偏移而阻塞或错位，
     必须仍从当前逻辑起点线性发送。
"""

from __future__ import annotations

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "app", "server"))

from air2dlna.ringbuffer import PcmRingBuffer          # noqa: E402
from air2dlna.stream import StreamManager, build_wav_header  # noqa: E402

BYTE_RATE = 44100 * 2 * 2          # S16LE / 44.1k / 2ch
SECONDS = 1.0
PCM_BYTES = int(BYTE_RATE * SECONDS)


class FakeWFile:
    """最小 wfile 替身：只收集字节。"""

    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, chunk: bytes) -> int:
        self.data.extend(chunk)
        return len(chunk)

    def flush(self) -> None:
        pass


def make_session(manager: StreamManager, ring: PcmRingBuffer, *, with_duration=True):
    """建一代流，并预填 1 秒 PCM。"""
    ring.flush()                      # generation 归 1
    pattern = bytes(range(256)) * (PCM_BYTES // 256 + 1)
    ring.append(pattern[:PCM_BYTES])
    session = manager.new_generation("wav", duration_ms=(SECONDS * 1000) if with_duration else None)
    return session, pattern[:PCM_BYTES]


class MultiConnectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ring = PcmRingBuffer(4 * 1024 * 1024)
        self.manager = StreamManager(self.ring)

    def test_repeated_gets_still_deliver_audio(self) -> None:
        """同一个 token 连续 6 次 GET，每一次都必须拿到音频。

        旧实现：第 2 次之后连接会立刻 break 并只补静音（body 为空）→ 失败。
        """
        session, pcm = make_session(self.manager, self.ring)

        for i in range(6):
            out = FakeWFile()
            self.manager.serve(session, out, idle_timeout=1.0)
            body = bytes(out.data)

            self.assertGreater(
                len(body), 44,
                f"第 {i + 1} 次 GET 没有拿到任何音频（body {len(body)} 字节）—— "
                f"这正是真机上「暂停/拖进度条后没声音」的表现")
            # WAV 头必须在（HTTP 层声明的是 200 + 完整长度）
            self.assertEqual(body[:4], b"RIFF", f"第 {i + 1} 次 GET 缺少 RIFF 头")
            self.assertEqual(body[8:12], b"WAVE", f"第 {i + 1} 次 GET 缺少 WAVE 标识")
            # 头之后的 PCM 必须与我们写入的一致
            self.assertEqual(
                body[44:44 + 256], pcm[:256],
                f"第 {i + 1} 次 GET 的 PCM 内容与写入不符")

    def test_second_connection_gets_full_track_not_remainder(self) -> None:
        """第二次连接应拿到完整 1 秒音频，而不是「total - 上次已发」的残量。"""
        session, _pcm = make_session(self.manager, self.ring)

        first = FakeWFile()
        self.manager.serve(session, first, idle_timeout=1.0)
        second = FakeWFile()
        self.manager.serve(session, second, idle_timeout=1.0)

        self.assertAlmostEqual(len(first.data), 44 + PCM_BYTES, delta=4096)
        self.assertAlmostEqual(
            len(second.data), 44 + PCM_BYTES, delta=4096,
            msg="第二次连接只拿到剩余字节（会话级计数导致），应为完整一代音频")

    def test_range_probe_does_not_block_or_shift(self) -> None:
        """Range 探测（44 / 1030600）必须立即返回音频，且不得错位。"""
        session, pcm = make_session(self.manager, self.ring)

        for range_start in (44, 1030600):
            out = FakeWFile()
            done = threading.Event()

            def run():
                self.manager.serve(session, out, range_start=range_start, idle_timeout=1.0)
                done.set()

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            finished = done.wait(timeout=8.0)

            self.assertTrue(
                finished,
                f"range_start={range_start} 时连接被阻塞（旧实现会去读尚不存在的环形偏移）")
            body = bytes(out.data)
            self.assertGreater(len(body), 44, f"range_start={range_start} 未返回音频")
            # 仍从逻辑起点发送：头之后的 PCM 必须与写入一致（不能错位 44 字节）
            self.assertEqual(body[44:44 + 256], pcm[:256],
                             f"range_start={range_start} 时 PCM 起点错位")

    def test_unknown_duration_streams_without_length(self) -> None:
        """时长未知时 total_bytes 为 None：不得因为计数而提前结束。"""
        session, pcm = make_session(self.manager, self.ring, with_duration=False)
        self.assertIsNone(session.total_bytes)
        out = FakeWFile()
        self.manager.serve(session, out, idle_timeout=1.0)
        self.assertGreater(len(out.data), 44)
        self.assertEqual(out.data[:4], b"RIFF")
        self.assertEqual(bytes(out.data[44:44 + 256]), pcm[:256])


class WavHeaderTest(unittest.TestCase):
    def test_header_declares_data_size(self) -> None:
        header = build_wav_header(PCM_BYTES)
        self.assertEqual(len(header), 44)
        self.assertEqual(header[:4], b"RIFF")
        self.assertEqual(int.from_bytes(header[40:44], "little"), PCM_BYTES)
        self.assertEqual(int.from_bytes(header[24:28], "little"), 44100)

    def test_header_unknown_size_uses_huge_constant(self) -> None:
        header = build_wav_header(None)
        self.assertEqual(int.from_bytes(header[40:44], "little"), 0xFFFFFF00)


if __name__ == "__main__":
    unittest.main(verbosity=2)
