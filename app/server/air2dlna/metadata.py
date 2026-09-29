"""shairport-sync 元数据管道解析器。

飞牛应用通过 ``metadata.pipe_name`` 读取 shairport-sync 的事件流（控制、时序、元数据）。
线格式已按 5.5.1 源码 ``metadata/pipe.c`` 核实::

    <item><type>%x</type><code>%x</code><length>%u</length>
    <data encoding="base64">
    <每行 76 字符的 base64>
    </data></item>

注意两点：

1. ``type`` 与 ``code`` 是 4 字符常量的**十六进制**数值（``%x``），
   例如 ``'pbeg'`` → ``70626567``，``'ssnc'`` → ``73736e63``。
   本解析器**同时兼容**旧文档中的字面 4 字符形式，两种都归一化为 4 字符字符串。
2. ``length`` 是 **base64 解码前**的原始长度；所有载荷都经 base64 编码，
   即使内容是纯文本（如 ``prgr``、``phbt``、``pvol``）。
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import select
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger("metadata")

_ITEM_START = b"<item>"
_ITEM_END = b"</item>"
_DATA_OPEN = b'<data encoding="base64">'
_DATA_CLOSE = b"</data>"


def normalize_code(raw: str) -> str:
    """把 ``"70626567"`` 或 ``"pbeg"`` 都归一化为 ``"pbeg"``。"""
    text = (raw or "").strip()
    if len(text) == 8:
        try:
            decoded = bytes.fromhex(text).decode("latin-1")
            if len(decoded) == 4 and all(32 <= ord(c) < 127 for c in decoded):
                return decoded
        except ValueError:
            pass
    return text


def _extract_tag(blob: bytes, tag: bytes) -> bytes:
    open_tag = b"<" + tag + b">"
    close_tag = b"</" + tag + b">"
    start = blob.find(open_tag)
    if start < 0:
        return b""
    start += len(open_tag)
    end = blob.find(close_tag, start)
    if end < 0:
        return b""
    return blob[start:end]


@dataclass
class MetadataItem:
    """一条元数据事件。"""

    type: str
    code: str
    length: int
    data: bytes
    received_ns: int = field(default_factory=time.monotonic_ns)

    @property
    def text(self) -> str:
        """载荷按 UTF-8 解码（失败则替换非法字节）。"""
        return self.data.decode("utf-8", "replace").strip()

    def uint32(self) -> Optional[int]:
        """把 4 字节载荷按**大端**解释为无符号整数（``astm`` 等二进制字段）。"""
        if len(self.data) == 4:
            return int.from_bytes(self.data, "big")
        try:
            return int(self.text)
        except (ValueError, TypeError):
            return None

    def csv_numbers(self) -> list[float]:
        """解析 ``"a,b,c,d"`` 形式的数字列表（``pvol``）。"""
        out: list[float] = []
        for part in self.text.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(float(part))
            except ValueError:
                return []
        return out

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"MetadataItem({self.type}/{self.code}, len={self.length}, data={self.data[:32]!r})"


def parse_items(buffer: bytearray) -> list[MetadataItem]:
    """从 ``buffer`` 中取出所有完整的 item，并就地删除已消费的字节。

    未闭合的尾部数据保留在 ``buffer`` 中等待后续数据。
    """
    items: list[MetadataItem] = []
    while True:
        start = buffer.find(_ITEM_START)
        if start < 0:
            # 丢弃明显不是 item 开头的垃圾，避免缓冲无限增长
            if len(buffer) > 8192:
                del buffer[:-16]
            return items
        if start > 0:
            del buffer[:start]
        end = buffer.find(_ITEM_END)
        if end < 0:
            # 一个 item 最长限制，防御性截断
            if len(buffer) > 1024 * 1024:
                del buffer[:-64]
            return items
        blob = bytes(buffer[: end + len(_ITEM_END)])
        del buffer[: end + len(_ITEM_END)]
        item = _parse_one(blob)
        if item is not None:
            items.append(item)


def _parse_one(blob: bytes) -> Optional[MetadataItem]:
    try:
        raw_type = _extract_tag(blob, b"type").decode("ascii", "replace")
        raw_code = _extract_tag(blob, b"code").decode("ascii", "replace")
        raw_length = _extract_tag(blob, b"length").decode("ascii", "replace")
        length = int(raw_length or "0")
    except (ValueError, UnicodeDecodeError):
        log.debug("跳过无法解析的元数据 item: %r", blob[:120])
        return None

    data = b""
    data_open = blob.find(_DATA_OPEN)
    if data_open >= 0:
        data_start = data_open + len(_DATA_OPEN)
        data_end = blob.find(_DATA_CLOSE, data_start)
        if data_end < 0:
            data_end = len(blob)
        encoded = bytes(blob[data_start:data_end])
        encoded = b"".join(encoded.split())  # 去掉换行与空白
        if encoded:
            try:
                data = base64.b64decode(encoded, validate=False)
            except (binascii.Error, ValueError):
                log.debug("元数据 base64 解码失败，载荷长度 %d", len(encoded))
                data = b""

    return MetadataItem(
        type=normalize_code(raw_type),
        code=normalize_code(raw_code),
        length=length,
        data=data,
    )


class MetadataPipeReader(threading.Thread):
    """从 FIFO 持续读取并解析元数据事件。

    使用 ``O_RDWR`` 打开 FIFO：Linux 上对 FIFO 而言 ``O_RDWR`` 永不阻塞、
    也不会在写端关闭时看到 EOF，因此读循环最简单可靠；同时保证 shairport-sync
    的写端随时可以打开管道。
    """

    def __init__(self, path: str, on_item: Callable[[MetadataItem], None],
                 poll_interval: float = 0.25) -> None:
        super().__init__(name="metadata-reader", daemon=True)
        self.path = path
        self.on_item = on_item
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._fd: Optional[int] = None
        self.items_seen = 0
        self.parse_errors = 0

    def stop(self) -> None:
        self._stop_event.set()

    def _close_fd(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def _open(self) -> bool:
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
            log.info("元数据管道已打开: %s", self.path)
            return True
        except OSError as exc:
            log.warning("打开元数据管道失败（%s），1 秒后重试: %s", exc, self.path)
            return False

    def run(self) -> None:
        buffer = bytearray()
        while not self._stop_event.is_set():
            if self._fd is None:
                if not self._open():
                    self._stop_event.wait(1.0)
                    continue
            try:
                ready, _, _ = select.select([self._fd], [], [], self.poll_interval)
            except (OSError, ValueError):
                self._close_fd()
                continue
            if not ready:
                continue
            try:
                chunk = os.read(self._fd, 65536)
            except BlockingIOError:
                continue
            except OSError as exc:
                log.warning("读取元数据管道出错（%s），重新打开", exc)
                self._close_fd()
                continue
            if not chunk:
                # O_RDWR 下正常不会发生；保险起见让出 CPU
                self._stop_event.wait(0.05)
                continue
            buffer.extend(chunk)
            try:
                items = parse_items(buffer)
            except Exception:  # noqa: BLE001 - 解析器绝不能让读线程退出
                self.parse_errors += 1
                log.exception("元数据解析异常，已丢弃当前缓冲")
                buffer.clear()
                continue
            for item in items:
                self.items_seen += 1
                try:
                    self.on_item(item)
                except Exception:  # noqa: BLE001
                    log.exception("元数据处理回调异常: %s/%s", item.type, item.code)
        self._close_fd()
        log.info("元数据管道读取线程结束（共 %d 条事件）", self.items_seen)


# --------------------------------------------------------------------- 便捷映射
#: ``core`` 类型中与展示相关的字段
CORE_FIELDS = {
    "asar": "artist",
    "asal": "album",
    "minm": "title",
    "assl": "album_artist",
    "asgn": "genre",
    "ascm": "comment",
    "ascp": "composer",
}

#: 全部已知的 ``ssnc`` 事件码（用于日志友好输出）
SSNC_CODES = {
    "pbeg": "播放开始",
    "pend": "播放结束",
    "pfls": "播放刷新(seek)",
    "paus": "暂停",
    "pres": "恢复",
    "prsm": "恢复(旧)",
    "prgr": "进度",
    "phbt": "进度心跳",
    "phb0": "首帧心跳",
    "pffr": "首帧计时",
    "pdis": "时间戳不连续",
    "pvol": "音量",
    "PICT": "封面",
    "clip": "客户端占用",
    "conn": "客户端连接",
    "disc": "客户端断开",
    "snam": "客户端名称",
    "snua": "客户端 UA",
    "svna": "服务名",
    "svip": "服务 IP",
    "daid": "DACP-ID",
    "acre": "Active-Remote",
    "dapo": "远程控制端口",
    "abeg": "进入活动模式",
    "aend": "退出活动模式",
    "mdst": "元数据开始",
    "mden": "元数据结束",
    "pcst": "封面开始",
    "pcen": "封面结束",
    "sdsc": "流描述",
    "styp": "流类型",
}

#: 载荷为纯文本的事件码（base64 解码后即为可读字符串）
TEXT_CODES = {
    "pfls", "prgr", "phbt", "phb0", "pffr", "pdis", "pvol",
    "clip", "conn", "disc", "snam", "snua", "svna", "svip",
    "daid", "acre", "dapo", "sdsc", "styp",
}
