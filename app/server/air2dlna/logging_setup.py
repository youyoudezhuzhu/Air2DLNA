"""日志：控制台 + 按大小轮转的文件日志。

飞牛应用的日志放在 ``TRIM_PKGVAR``。Web UI 的「日志」面板直接读取该文件尾部。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from collections import deque

_LOG_FORMAT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}


class RingLogHandler(logging.Handler):
    """在内存中保留最近若干条日志，供 Web UI 快速读取（避免频繁读盘）。"""

    def __init__(self, capacity: int = 2000) -> None:
        super().__init__()
        self._lines: deque[str] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            self._lines.append(line)

    def tail(self, count: int = 200) -> list[str]:
        with self._lock:
            return list(self._lines)[-count:]


_ring = RingLogHandler()


def get_ring_handler() -> RingLogHandler:
    return _ring


def setup_logging(log_path: str, level: str = "info", max_bytes: int = 5 * 1024 * 1024,
                  backups: int = 5) -> logging.Logger:
    """初始化根日志器。幂等：重复调用只调整级别。"""
    root = logging.getLogger()
    root.setLevel(_LEVELS.get(str(level).lower(), logging.INFO))

    if getattr(root, "_a2d_configured", False):
        return root

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    directory = os.path.dirname(os.path.abspath(log_path)) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass

    file_handler: logging.Handler
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
    except OSError:
        # 磁盘不可写时退化为仅控制台，服务仍要能起来
        file_handler = logging.StreamHandler(sys.stderr)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)

    _ring.setFormatter(formatter)
    root.addHandler(_ring)

    # 第三方库（http.server 等）降噪
    logging.getLogger("http.server").setLevel(logging.WARNING)

    root._a2d_configured = True  # type: ignore[attr-defined]
    return root


def set_level(level: str) -> None:
    logging.getLogger().setLevel(_LEVELS.get(str(level).lower(), logging.INFO))
