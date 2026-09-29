"""日志轮转单元测试。

覆盖 1.0.1 修复：main.log / bridge-stderr.log / shairport-sync.log / nqptp.log
此前没有任何清理逻辑，会随运行时间无限增长。现在统一为「超过上限只保留尾部」，
且必须是**原地重写**（不能 rename）—— 写入方以 O_APPEND 持有文件描述符，
rename 会让它继续写进已改名的旧 inode。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "app" / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from air2dlna import supervisor  # noqa: E402


class TrimLogFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "test.log")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, data: bytes) -> None:
        with open(self.path, "wb") as handle:
            handle.write(data)

    def test_small_file_untouched(self) -> None:
        self._write(b"line1\nline2\n")
        freed = supervisor.trim_log_file(self.path, max_bytes=1024, keep_bytes=256)
        self.assertEqual(0, freed)
        self.assertEqual(b"line1\nline2\n", open(self.path, "rb").read())

    def test_missing_file_is_noop(self) -> None:
        self.assertEqual(0, supervisor.trim_log_file(self.path, 1024, 256))

    def test_oversized_file_keeps_configured_tail(self) -> None:
        # 每行固定长度，便于断言保留了哪些内容
        body = b"".join(f"line-{i:06d}\n".encode() for i in range(5000))  # ≈ 60KB
        self._write(body)
        freed = supervisor.trim_log_file(self.path, max_bytes=10240, keep_bytes=2048)
        self.assertGreater(freed, 0)

        data = open(self.path, "rb").read()
        self.assertLessEqual(len(data), 2048 + 200, "轮转后应接近 keep_bytes 量级")
        self.assertTrue(data.startswith(b"[logrotate]"), "应写入轮转标记行")
        # 尾部内容必须保留：原文件的最后一行仍在，且位置正确（末尾）
        self.assertTrue(data.rstrip(b"\n").endswith(b"line-004999"))

    def test_inode_preserved_for_append_writers(self) -> None:
        """核心回归点：必须原地重写，inode 不变（否则 O_APPEND 写入方会写进旧 inode）。"""
        body = b"x" * 40000
        self._write(body)
        inode_before = os.stat(self.path).st_ino
        supervisor.trim_log_file(self.path, max_bytes=1024, keep_bytes=512)
        self.assertEqual(inode_before, os.stat(self.path).st_ino)

        # 模拟以 O_APPEND 打开的写入方（子进程 stdout / shell >>）：轮转后仍能正常追加
        with open(self.path, "ab") as handle:
            handle.write(b"appended-after-rotation\n")
        data = open(self.path, "rb").read()
        self.assertTrue(data.endswith(b"appended-after-rotation\n"))

    def test_idempotent_when_below_limit_after_rotation(self) -> None:
        self._write(b"y" * 40000)
        supervisor.trim_log_file(self.path, max_bytes=1024, keep_bytes=512)
        size = os.path.getsize(self.path)
        freed = supervisor.trim_log_file(self.path, max_bytes=1024, keep_bytes=512)
        self.assertEqual(0, freed, "已在限额内不应再次轮转")
        self.assertEqual(size, os.path.getsize(self.path))

    def test_defaults_are_bounded(self) -> None:
        """默认上限必须是有限值，避免再次出现无上限增长。"""
        self.assertLessEqual(supervisor.LOG_MAX_BYTES, 32 * 1024 * 1024)
        self.assertLess(supervisor.LOG_KEEP_BYTES, supervisor.LOG_MAX_BYTES)


class SupervisorRotationWiringTests(unittest.TestCase):
    def test_supervisor_declares_bounded_log_policy(self) -> None:
        sup = supervisor.ProcessSupervisor(
            name="demo", argv=["/bin/true"], log_path="/tmp/demo.log", enabled=False
        )
        self.assertEqual(supervisor.LOG_MAX_BYTES, sup.log_max_bytes)
        self.assertEqual(supervisor.LOG_KEEP_BYTES, sup.log_keep_bytes)
        self.assertGreater(sup.LOG_CHECK_EVERY, 0)
        self.assertTrue(hasattr(sup, "_tick_log_rotation"), "看护循环应挂上日志轮转")


class ShellScriptsCoverAllLogsTests(unittest.TestCase):
    """shell 侧重定向的日志（main.log / bridge-stderr.log / nqptp.log）也要有轮转。"""

    def _read(self, rel: str) -> str:
        return (REPO_ROOT / rel).read_text(encoding="utf-8")

    def test_cmd_main_trims_both_logs(self) -> None:
        text = self._read("cmd/main")
        self.assertIn("trim_log()", text)
        self.assertIn('trim_log "$MAIN_LOG"', text)
        self.assertIn('trim_log "$BRIDGE_STDERR_LOG"', text)
        # bridge 的 stdout/stderr 不再灌进 main.log（历史上造成双份写入）
        self.assertIn('exec >>"$BRIDGE_STDERR_LOG" 2>&1', text)
        self.assertNotIn('exec >>"$MAIN_LOG" 2>&1', text)

    def test_watchdog_trims_its_logs_periodically(self) -> None:
        text = self._read("app/server/nqptp-watchdog.sh")
        self.assertIn("trim_log()", text)
        self.assertIn('trim_log "$LOG"', text)
        self.assertIn("tick", text)


if __name__ == "__main__":
    unittest.main()
