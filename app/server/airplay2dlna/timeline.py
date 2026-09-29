"""AirPlay 音频时间线（AudioTimeline）。

对应 TECHNICAL_DESIGN 第 9 / 13 节。三个时间基准：

* AirPlay RTP 时间戳（``prgr``、``phbt``/``phb0`` 载荷）
* AirPlay 单调时钟 ``CLOCK_MONOTONIC_RAW``（``phbt`` 的第二个字段，纳秒）
* DLNA 渲染器自报的流内位置（``GetPositionInfo.RelTime`` / GENA）

已核实的载荷语义（shairport-sync 5.5.1 源码）：

* ``prgr``  ——  纯文本 ``"<rtpstampstart>/<rtpstampnow>/<rtpstampend> <rate>"``
* ``phbt``  ——  纯文本 ``"<rtp_timestamp>/<should_be_time_ns>"``，周期发送
* ``phb0``  ——  同 ``phbt``，但只在会话首帧发送
* ``pffr``  ——  纯文本 ``"<frame_number>/<time_it_should_be_played_ns>"``

于是任意单调时刻 ``t`` 的曲目位置为::

    pos(t) = (rtp_anchor - rtpstampstart) / rate + (t - anchor_mono_ns) / 1e9

``phbt`` 的 ``should_be_time`` 是「该帧应当被播放」的时刻，因此 ``pos(t)`` 给出的是
**应当被听到**的位置，而不是「刚被写入缓冲」的位置，正是桥接需要的真值。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

log = logging.getLogger("timeline")


def monotonic_raw_ns() -> int:
    """``CLOCK_MONOTONIC_RAW`` 纳秒；不可用时退回 ``CLOCK_MONOTONIC``。"""
    for clock in (getattr(time, "CLOCK_MONOTONIC_RAW", None), time.CLOCK_MONOTONIC):
        if clock is None:
            continue
        try:
            return time.clock_gettime_ns(clock)
        except (OSError, AttributeError, ValueError):
            continue
    return time.monotonic_ns()


def _parse_pair(payload: str) -> Optional[tuple[int, int]]:
    """解析 ``"a/b"`` 形式的两个整数。"""
    try:
        left, right = payload.strip().split("/", 1)
        return int(left.strip()), int(right.strip())
    except (ValueError, AttributeError):
        return None


def _parse_progress(payload: str) -> Optional[tuple[int, int, int, int]]:
    """解析 ``prgr`` 载荷 ``"<start>/<now>/<end> <rate>"``。"""
    try:
        parts = payload.strip().split()
        stamps = parts[0].split("/")
        if len(stamps) != 3:
            return None
        start, now, end = (int(x) for x in stamps)
        rate = int(float(parts[1])) if len(parts) > 1 else 0
        return start, now, end, rate
    except (ValueError, IndexError, AttributeError):
        return None


class AudioTimeline:
    """线程安全的时间线状态机。"""

    def __init__(self, sample_rate: int = 44100) -> None:
        self._lock = threading.RLock()
        self._rate = int(sample_rate)

        # prgr
        self._rtp_start: Optional[int] = None
        self._rtp_now: Optional[int] = None
        self._rtp_end: Optional[int] = None
        self._prgr_mono_ns: Optional[int] = None

        # phbt / phb0 / pffr 锚点
        self._anchor_rtp: Optional[int] = None
        self._anchor_mono_ns: Optional[int] = None
        self._first_frame_rtp: Optional[int] = None
        self._first_frame_mono_ns: Optional[int] = None

        # 播放状态
        self._playing = False
        self._paused_at_ms: Optional[float] = None

        # 代（generation）偏移
        self._gen: Optional[int] = None
        self._gen_offset_ms: float = 0.0
        self._gen_offset_frozen = False

        # 渲染器观测
        self._rel_time_ms: Optional[float] = None
        self._renderer_latency_ms: float = 0.0

        # 元数据回退
        self._metadata_duration_ms: Optional[float] = None

        self._anchor_rejected_logged = False

    # ------------------------------------------------------------------ 设置
    def set_rate(self, rate: int) -> None:
        if rate and rate > 0:
            with self._lock:
                self._rate = int(rate)

    @property
    def rate(self) -> int:
        with self._lock:
            return self._rate

    def set_renderer_latency_ms(self, latency_ms: float) -> None:
        with self._lock:
            self._renderer_latency_ms = float(latency_ms)

    def set_metadata_duration_ms(self, duration_ms: Optional[float]) -> None:
        with self._lock:
            self._metadata_duration_ms = duration_ms

    # ------------------------------------------------------- AirPlay 事件入口
    def on_prgr(self, payload: str) -> None:
        """收到 ``prgr``（每秒一次，权威的位置/时长来源）。"""
        parsed = _parse_progress(payload)
        if not parsed:
            log.debug("无法解析 prgr 载荷: %r", payload)
            return
        start, now, end, rate = parsed
        with self._lock:
            self._rtp_start = start
            self._rtp_now = now
            self._rtp_end = end
            self._prgr_mono_ns = monotonic_raw_ns()
            if rate > 0:
                self._rate = rate
            log.debug(
                "prgr: start=%s now=%s end=%s rate=%s -> 位置 %.1fs / 时长 %.1fs",
                start, now, end, rate,
                (now - start) / float(self._rate),
                (end - start) / float(self._rate) if end > start else 0.0,
            )

    def on_phbt(self, payload: str, first: bool = False) -> None:
        """收到 ``phbt`` / ``phb0``。"""
        parsed = _parse_pair(payload)
        if not parsed:
            log.debug("无法解析 phbt 载荷: %r", payload)
            return
        rtp, mono_ns = parsed
        # 防御：should_be_time 应基于 CLOCK_MONOTONIC_RAW，与实际时刻相差应在数秒内。
        # 若相差过大（时钟基准不一致或数据异常），拒绝该锚点而不是让位置计算出荒谬值。
        skew_ns = mono_ns - monotonic_raw_ns()
        if abs(skew_ns) > 60 * 10**9:
            if not self._anchor_rejected_logged:
                log.warning(
                    "拒绝异常 phbt 锚点：should_be_time 与本地单调时钟相差 %.1f 秒"
                    "（可能的时钟基准不一致）",
                    skew_ns / 1e9,
                )
                self._anchor_rejected_logged = True
            return
        with self._lock:
            self._anchor_rtp = rtp
            self._anchor_mono_ns = mono_ns
            if first:
                self._first_frame_rtp = rtp
                self._first_frame_mono_ns = mono_ns

    def on_pffr(self, payload: str) -> None:
        """收到 ``pffr``（首帧已被有效计时）。"""
        parsed = _parse_pair(payload)
        if not parsed:
            return
        frame, mono_ns = parsed
        with self._lock:
            if self._anchor_rtp is None:
                self._anchor_rtp = frame
                self._anchor_mono_ns = mono_ns
            self._first_frame_rtp = frame
            self._first_frame_mono_ns = mono_ns

    # ------------------------------------------------------------ 状态迁移
    def on_play_begin(self) -> None:
        with self._lock:
            self._playing = True
            self._paused_at_ms = None

    def on_resume(self) -> None:
        with self._lock:
            self._playing = True
            if self._paused_at_ms is not None:
                # 恢复后旧锚点的单调时钟已经不准，丢弃锚点，等待新的 phbt/prgr
                self._anchor_mono_ns = None
            self._paused_at_ms = None

    def on_pause(self) -> None:
        with self._lock:
            if self._playing:
                self._paused_at_ms = self._position_locked()
            self._playing = False

    def on_stop(self) -> None:
        with self._lock:
            self._playing = False
            self._paused_at_ms = None
            self._rtp_start = None
            self._rtp_now = None
            self._rtp_end = None
            self._anchor_rtp = None
            self._anchor_mono_ns = None
            self._first_frame_rtp = None
            self._first_frame_mono_ns = None
            self._rel_time_ms = None
            self._metadata_duration_ms = None
            self._gen_offset_frozen = False

    def reset_for_new_session(self) -> None:
        """新会话（``conn``/``pbeg``）时完全重置，但保留采样率。"""
        self.on_stop()

    # ---------------------------------------------------------- 代偏移管理
    def begin_generation(self, generation: int, offset_ms: Optional[float] = None) -> None:
        """新建一代 DLNA 流。

        ``offset_ms`` 为该代第 0 字节对应的曲目位置；为 None 时用当前估计值。
        """
        with self._lock:
            self._gen = int(generation)
            if offset_ms is None:
                offset_ms = self._position_locked() or 0.0
            self._gen_offset_ms = max(0.0, float(offset_ms))
            self._gen_offset_frozen = False
            self._rel_time_ms = None
            log.info("新建音频代 gen=%s，起始曲目位置 %.0f ms", generation, self._gen_offset_ms)

    def refine_generation_offset(self, offset_ms: Optional[float]) -> None:
        """在渲染器真正开始出声前，用更准的位置信息修正代偏移。"""
        if offset_ms is None:
            return
        with self._lock:
            if self._gen_offset_frozen:
                return
            delta = abs(offset_ms - self._gen_offset_ms)
            if delta > 1.0:
                log.debug(
                    "修正代偏移 %.0f ms -> %.0f ms (gen=%s)",
                    self._gen_offset_ms, offset_ms, self._gen,
                )
            self._gen_offset_ms = max(0.0, float(offset_ms))

    def freeze_generation_offset(self) -> None:
        with self._lock:
            if not self._gen_offset_frozen:
                self._gen_offset_frozen = True
                log.debug("冻结代偏移 = %.0f ms (gen=%s)", self._gen_offset_ms, self._gen)

    @property
    def generation_offset_ms(self) -> float:
        with self._lock:
            return self._gen_offset_ms

    # -------------------------------------------------------- 渲染器观测值
    def update_renderer_position(self, rel_time_ms: Optional[float]) -> None:
        with self._lock:
            self._rel_time_ms = rel_time_ms
            if rel_time_ms is not None and rel_time_ms > 0:
                # 渲染器已经开始出声，代偏移不再修正
                self._gen_offset_frozen = True

    @property
    def renderer_rel_time_ms(self) -> Optional[float]:
        with self._lock:
            return self._rel_time_ms

    # ------------------------------------------------------------- 位置查询
    def _position_locked(self) -> Optional[float]:
        """无锁版本，调用方必须已持有 ``self._lock``。"""
        if not self._playing and self._paused_at_ms is not None:
            return self._paused_at_ms

        # 首选：RTP + 单调时钟锚点
        if (
            self._playing
            and self._anchor_rtp is not None
            and self._anchor_mono_ns is not None
            and self._rtp_start is not None
        ):
            elapsed_ns = monotonic_raw_ns() - self._anchor_mono_ns
            base_sec = (self._anchor_rtp - self._rtp_start) / float(self._rate)
            position_ms = (base_sec + elapsed_ns / 1e9) * 1000.0
            return self._clamp_locked(position_ms)

        # 次选：代偏移 + 渲染器自报位置
        if self._rel_time_ms is not None:
            return self._clamp_locked(
                self._gen_offset_ms + self._rel_time_ms + self._renderer_latency_ms
            )

        # 再次：prgr 的瞬时值（不含插值）
        if self._rtp_start is not None and self._rtp_now is not None:
            return self._clamp_locked(
                (self._rtp_now - self._rtp_start) / float(self._rate) * 1000.0
            )

        # 最后：仅代偏移
        if self._gen_offset_ms:
            return self._clamp_locked(self._gen_offset_ms)
        return None

    def _clamp_locked(self, position_ms: float) -> float:
        if position_ms < 0:
            return 0.0
        duration = self._duration_locked()
        if duration and position_ms > duration:
            return duration
        return position_ms

    def _duration_locked(self) -> Optional[float]:
        if (
            self._rtp_start is not None
            and self._rtp_end is not None
            and self._rtp_end > self._rtp_start
        ):
            return (self._rtp_end - self._rtp_start) / float(self._rate) * 1000.0
        return self._metadata_duration_ms

    def position_ms(self) -> Optional[float]:
        with self._lock:
            return self._position_locked()

    def duration_ms(self) -> Optional[float]:
        with self._lock:
            return self._duration_locked()

    @property
    def playing(self) -> bool:
        with self._lock:
            return self._playing

    def has_anchor(self) -> bool:
        with self._lock:
            return self._anchor_rtp is not None and self._anchor_mono_ns is not None

    # --------------------------------------------------------------- 漂移
    def drift_ms(self) -> Optional[float]:
        """渲染器实际位置与内部时间线的偏差（毫秒，正数表示渲染器偏慢）。"""
        with self._lock:
            if self._rel_time_ms is None:
                return None
            expected = self._position_locked()
            if expected is None:
                return None
            observed = self._gen_offset_ms + self._rel_time_ms + self._renderer_latency_ms
            return expected - observed

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "position_ms": self._position_locked(),
                "duration_ms": self._duration_locked(),
                "playing": self._playing,
                "generation": self._gen,
                "generation_offset_ms": self._gen_offset_ms,
                "generation_offset_frozen": self._gen_offset_frozen,
                "renderer_rel_time_ms": self._rel_time_ms,
                "anchor": (
                    {"rtp": self._anchor_rtp, "mono_ns": self._anchor_mono_ns}
                    if self._anchor_rtp is not None
                    else None
                ),
                "rtp_start": self._rtp_start,
                "rtp_end": self._rtp_end,
                "rate": self._rate,
                "drift_ms": self.drift_ms(),
            }
