"""PCM 环形缓冲。

生产者：读取 shairport-sync 音频 FIFO 的线程（单生产者）。
消费者：向 DLNA 渲染器提供 HTTP 流的线程（可能有多个，通常 1 个）。

设计要点（对应 TECHNICAL_DESIGN 第 6 节）：

* 使用「绝对字节偏移」而不是相对位置，读者各自记录自己的 ``read_offset``。
* ``generation``（代）是处理 seek / 换曲 / 会话重建的核心机制：一旦发生不连续，
  ``gen`` 自增并且写入偏移归零。持有旧代的读者立刻收到 :class:`StaleGeneration`，
  其 HTTP 连接被主动关闭，促使渲染器重新 GET，从而从新位置开始播放。
* 容量有界：渲染器长期不消费时数据被覆盖，慢读者收到 :class:`BufferOverflow`，
  不会出现内存无限增长。
"""

from __future__ import annotations

import threading


class StaleGeneration(Exception):
    """读者持有的 generation 已过期（发生了 seek / 换曲）。"""


class BufferOverflow(Exception):
    """读者的读取位置已被覆盖（消费太慢）。"""


class PcmRingBuffer:
    """定长环形 PCM 缓冲。线程安全。"""

    def __init__(self, capacity_bytes: int, sample_rate: int = 44100,
                 channels: int = 2, sample_width: int = 2) -> None:
        self.capacity = max(int(capacity_bytes), 65536)
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.sample_width = int(sample_width)
        self.frame_bytes = self.channels * self.sample_width
        self.byte_rate = self.sample_rate * self.frame_bytes

        self._buf = bytearray(self.capacity)
        self._capacity = self.capacity
        self._write = 0          # 当前代内已写入的绝对字节数
        self._gen = 0
        self._closed = False
        self._cond = threading.Condition()
        # 统计
        self._total_bytes = 0    # 跨代累计写入
        self._overflows = 0
        self._stale_evictions = 0

    # ------------------------------------------------------------------ 属性
    @property
    def generation(self) -> int:
        with self._cond:
            return self._gen

    @property
    def write_offset(self) -> int:
        with self._cond:
            return self._write

    @property
    def capacity_seconds(self) -> float:
        return self._capacity / float(self.byte_rate)

    def stats(self) -> dict:
        with self._cond:
            return {
                "generation": self._gen,
                "write_offset": self._write,
                "capacity_bytes": self._capacity,
                "buffered_seconds": self._write / float(self.byte_rate),
                "total_bytes": self._total_bytes,
                "overflows": self._overflows,
                "stale_evictions": self._stale_evictions,
                "closed": self._closed,
            }

    @staticmethod
    def bytes_to_seconds(num_bytes: int, byte_rate: int) -> float:
        if byte_rate <= 0:
            return 0.0
        return num_bytes / float(byte_rate)

    # ------------------------------------------------------------------ 写入
    def append(self, data: bytes) -> None:
        """追加 PCM 数据。若单次写入超过容量，只保留末尾部分。"""
        if not data:
            return
        with self._cond:
            if self._closed:
                return
            if len(data) > self._capacity:
                # 极端情况：一次性写入超过整个容量，丢弃早期数据并前移写指针
                dropped = len(data) - self._capacity
                data = data[dropped:]
                self._write += dropped
            count = len(data)
            start = self._write % self._capacity
            end = start + count
            if end <= self._capacity:
                self._buf[start:end] = data
            else:
                first = self._capacity - start
                self._buf[start:] = data[:first]
                self._buf[: count - first] = data[first:]
            self._write += count
            self._total_bytes += count
            self._cond.notify_all()

    def flush(self) -> int:
        """标记不连续：新开一代并清空。返回新的 generation。"""
        with self._cond:
            self._gen += 1
            self._write = 0
            self._cond.notify_all()
            return self._gen

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    # ------------------------------------------------------------------ 读取
    def read(self, offset: int, generation: int, max_bytes: int, timeout: float) -> tuple[bytes, int]:
        """从 ``offset`` 读取最多 ``max_bytes`` 字节。

        返回 ``(data, new_offset)``。超时且无新数据时返回 ``(b"", offset)``。
        """
        with self._cond:
            if generation != self._gen:
                self._stale_evictions += 1
                raise StaleGeneration(
                    f"缓冲已换代（持有 {generation}，当前 {self._gen}）"
                )
            if offset > self._write:
                # 数据还没产生（理论上不该发生），等待
                self._cond.wait(timeout)
                if generation != self._gen:
                    raise StaleGeneration("等待期间缓冲换代")
                if offset > self._write:
                    return b"", offset
            if offset < self._write - self._capacity:
                self._overflows += 1
                raise BufferOverflow(
                    f"读取位置 {offset} 已被覆盖（write={self._write}, cap={self._capacity}）"
                )
            if offset >= self._write:
                # 已追平，等待新数据
                deadline_reached = self._cond.wait(timeout)
                if generation != self._gen:
                    raise StaleGeneration("等待期间缓冲换代")
                if offset >= self._write:
                    return b"", offset
            count = min(max_bytes, self._write - offset)
            start = offset % self._capacity
            end = start + count
            if end <= self._capacity:
                data = bytes(self._buf[start:end])
            else:
                first = self._capacity - start
                data = bytes(self._buf[start:]) + bytes(self._buf[: count - first])
            return data, offset + count

    def wait_for_data(self, offset: int, generation: int, needed_bytes: int, timeout: float) -> bool:
        """等待 ``offset`` 起至少 ``needed_bytes`` 字节可用。"""
        with self._cond:
            if generation != self._gen:
                raise StaleGeneration("等待预滚动期间缓冲换代")
            deadline = timeout
            while self._write - offset < needed_bytes:
                if self._closed:
                    return False
                if not self._cond.wait(deadline):
                    return self._write - offset >= needed_bytes
                if generation != self._gen:
                    raise StaleGeneration("等待预滚动期间缓冲换代")
            return True
