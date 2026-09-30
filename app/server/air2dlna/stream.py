"""实时 PCM/WAV HTTP 流：DLNA 渲染器从这里拉取音频。

在 Virtual Player 架构（ARCHITECTURE_V2 第 24 节）里，本模块的角色是
**Virtual Media Output**：它对外表现为一个连续、稳定的媒体资源，内部可能来自
真实 AirPlay PCM，也可能来自输出层生成的静音。

对应 TECHNICAL_DESIGN 第 5、6 节。已按 AirConnect 1.12.4 的实战经验调整：

* 每一代流使用**唯一的路径**（``/stream/<token>.wav``）而不是查询串，
  因为部分渲染器会缓存 URI（AirConnect 使用 ``/stream-<n>.flac`` 正是这个原因）。
* WAV 头在**时长未知**时使用 AirConnect 经过实战验证的「超大长度」常量
  （``data=0xFFFFFF00``、``riff=0xFFFFFF24``），而不是 ``0xFFFFFFFF``（个别解析器会拒收）。
  在时长已知时写出**真实长度**，让渲染器能显示总时长与进度。
* 响应附带 DLNA 互操作头：``transferMode.dlna.org: Streaming``，
  并在渲染器请求 ``getcontentFeatures.dlna.org`` 时回 ``contentFeatures.dlna.org``。
* 对 ``Range`` 请求：``start == 0`` 时按「从当前位置完整重发」返回 200
  （即向渲染器声明不支持字节范围，这是 AirConnect 的成熟做法）；
  ``start > 0`` 且数据仍在缓冲中时返回 206 + ``Content-Range``。
* **连续输出**（``session.continuous_output``）：DLNA 仍在播放时，暂时没有真实
  AirPlay PCM 就用静音填充并保持响应，绝不 EOF；静音只在输出层生成，绝不写回
  AirPlay 环形缓冲（第 6/7 节）。
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .ringbuffer import BufferOverflow, PcmRingBuffer, StaleGeneration

log = logging.getLogger("stream")

#: DLNA 内容特性字符串（与 AirConnect 使用的常量一致）
DLNA_FLAGS = "DLNA.ORG_OP=00;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=0d500000000000000000000000000000"
FEATURES_WAV = f"http-get:*:audio/wav:{DLNA_FLAGS}"
FEATURES_L16 = f"http-get:*:audio/L16;rate=44100;channels=2:DLNA.ORG_PN=LPCM;{DLNA_FLAGS}"

#: 时长未知时的 WAV 长度常量（AirConnect 验证过的取值）
_FAKE_DATA_SIZE = 0xFFFFFF00
_FAKE_RIFF_SIZE = _FAKE_DATA_SIZE + 36


def build_wav_header(data_size: Optional[int], sample_rate: int = 44100,
                     channels: int = 2, bits: int = 16) -> bytes:
    """构造 44 字节 RIFF/WAVE(LPCM) 头。

    ``data_size`` 为 ``None`` 时使用超大常量表示「长度未知」。
    """
    block_align = channels * bits // 8
    byte_rate = sample_rate * block_align
    if data_size is None:
        declared_data = _FAKE_DATA_SIZE
        declared_riff = _FAKE_RIFF_SIZE
    else:
        declared_data = max(0, int(data_size))
        declared_riff = declared_data + 36
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", declared_riff,
        b"WAVE",
        b"fmt ", 16,
        1,                      # PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        b"data", declared_data,
    )


def format_didl_time(ms: Optional[float]) -> str:
    """毫秒 -> ``H:MM:SS.mmm``（DIDL ``res@duration`` 格式）。"""
    if ms is None or ms < 0:
        ms = 0
    total_ms = int(round(ms))
    hours, remainder = divmod(total_ms, 3600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def airplay_db_to_percent(db: float) -> tuple[int, bool]:
    """把 AirPlay 的 ``pvol`` 分贝值映射为 DLNA 的 0..100 音量。

    shairport-sync 文档：``airplay_volume`` 取值 0.00 → -30.00（在 iOS 音量滑块上
    线性），``-144.00`` 表示静音。因此按滑块位置线性映射即可与用户所见一致。
    返回 ``(百分比, 是否静音)``。
    """
    if db <= -144.0:
        return 0, True
    if db >= 0.0:
        return 100, False
    if db <= -30.0:
        return 0, False
    return int(round((db + 30.0) / 30.0 * 100.0)), False


def build_didl_lite(uri: str, protocol_info: str, title: str = "", artist: str = "",
                    album: str = "", artwork_url: str = "",
                    duration_ms: Optional[float] = None,
                    sample_rate: int = 44100, channels: int = 2, bits: int = 16) -> str:
    """构造 ``SetAVTransportURI`` 需要的 DIDL-Lite 元数据。

    时长未知时使用 ``object.item.audioItem.audioBroadcast``（直播语义），
    已知时使用 ``object.item.audioItem.musicTrack`` 并带上 ``res@duration``——
    这是渲染器显示进度条与标题的关键。
    """
    from xml.sax.saxutils import escape, quoteattr

    def text(value: str) -> str:
        return escape(value or "")

    known_duration = duration_ms is not None and duration_ms > 0
    upnp_class = (
        "object.item.audioItem.musicTrack" if known_duration
        else "object.item.audioItem.audioBroadcast"
    )

    res_attrs = [f"protocolInfo={quoteattr(protocol_info)}"]
    if known_duration:
        res_attrs.append(f"duration={quoteattr(format_didl_time(duration_ms))}")
        res_attrs.append(f"sampleFrequency={sample_rate}")
        res_attrs.append(f"bitsPerSample={bits}")
        res_attrs.append(f"nrAudioChannels={channels}")
        size = int(sample_rate * (bits // 8) * channels * (duration_ms / 1000.0))
        res_attrs.append(f"size={size}")

    parts = [
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"',
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"',
        ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"',
        ' xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">',
        '<item id="1" parentID="0" restricted="1">',
        f"<dc:title>{text(title)}</dc:title>",
        f"<dc:creator>{text(artist)}</dc:creator>",
        f"<upnp:artist>{text(artist)}</upnp:artist>",
        f"<upnp:album>{text(album)}</upnp:album>",
    ]
    if artwork_url:
        parts.append(f"<upnp:albumArtURI>{text(artwork_url)}</upnp:albumArtURI>")
    parts.append(f"<upnp:class>{upnp_class}</upnp:class>")
    parts.append(f"<res {' '.join(res_attrs)}>{text(uri)}</res>")
    parts.append("</item></DIDL-Lite>")
    return "".join(parts)


@dataclass
class StreamSession:
    """一代可被渲染器拉取的实时流。"""

    token: str
    generation: int
    kind: str                       # 'wav' | 'l16'
    content_type: str
    sample_rate: int = 44100
    channels: int = 2
    bits: int = 16
    duration_ms: Optional[float] = None
    created_at: float = field(default_factory=time.monotonic)
    last_activity: float = 0.0        # 最近一次成功向客户端写出数据的时刻
    total_bytes: Optional[int] = None     # 声明给渲染器的 data 长度（WAV）
    bytes_served: int = 0
    clients: int = 0
    closed: bool = False
    #: 诊断：本 token 被拉取的次数与最近一次 Range 起点（HTTP 层细节，不触发换代）
    range_requests: int = 0
    last_range_info: str = ""
    #: 暂停恢复（方案 A）：待把「逻辑字节 0」映射到的实际环形偏移。
    #: 仅在 pause recovery 期间由控制器设置；渲染器重连请求 Range: bytes=0- 时，
    #: 服务端从该偏移开始发送 PCM（它以为自己从头播，实际听到的是暂停位置之后的音频）。
    recovery_byte_offset: Optional[int] = None
    #: 该映射是否已经真正生效过（用于判定恢复成功）
    recovery_applied: bool = False
    #: 写出字节回调（控制器用它记录恢复耗时的字节里程碑；不参与业务逻辑）
    on_bytes: Optional[Callable[[int], None]] = None
    #: 建连回调（控制器用它记录恢复耗时里的 T8：渲染器对新 URI 发起 GET）
    on_connect: Optional[Callable[[], None]] = None
    #: 暂停 keepalive（方案 A）：为真时输出层改为持续发送静音 PCM。
    #: **静音只在输出层生成，绝不写入环形缓冲** —— 环形缓冲承载的是 AirPlay 真实
    #: 时间线，不能被合成音频污染（否则位置/seek/时间线全部失真）。
    silence_mode: bool = False
    #: 连续输出语义（ARCHITECTURE_V2 第 6 节）：为真时，只要 DLNA 还在播放，
    #: 暂时没有真实 AirPlay PCM 就用静音填充，**绝不 EOF / 断开响应**。
    #: 默认 False：只有 DLNA Output 层为「正在播放」的媒体会话打开它，
    #: 其它调用方（含单元测试）保持旧的 idle_timeout 断开语义。
    continuous_output: bool = False
    #: 连续输出的静音上限（秒）。累计静音超过该值后回调 ``on_silence_timeout``
    #: （由 Virtual Player 转入 RECOVERING），但连接本身**不会**被主动断开。
    #: 0 表示不触发（仅用于测试或明确要求无限静音的场景）。
    silence_timeout_s: float = 0.0
    #: 静音超时回调：只在输出线程里被调用一次，用于通知控制层「长时间没有
    #: 真实 PCM」。回调必须立即返回（不得做网络/阻塞操作）。
    on_silence_timeout: Optional[Callable[[], None]] = None
    #: 已经通过连续输出发送的静音字节（诊断用；与 ``bytes_served`` 分开统计）。
    silence_bytes: int = 0
    #: 连续输出下等待真实 PCM 的单次读取超时（秒）。越小，真实 PCM 断流后
    #: 越快地切换为静音（暂停/seek/切歌后的空窗因此不会变成 EOF）。
    read_timeout_s: float = 0.2

    @property
    def byte_rate(self) -> int:
        return self.sample_rate * self.channels * (self.bits // 8)

    @property
    def extension(self) -> str:
        return "wav" if self.kind == "wav" else "pcm"

    @property
    def protocol_info(self) -> str:
        return FEATURES_WAV if self.kind == "wav" else FEATURES_L16

    def path(self, prefix: str = "/stream") -> str:
        return f"{prefix}/{self.token}.{self.extension}"


class StreamManager:
    """管理「当前代」的流会话，并把环形缓冲写给 HTTP 客户端。"""

    def __init__(self, ring: PcmRingBuffer, sample_rate: int = 44100,
                 channels: int = 2, bits: int = 16) -> None:
        self._lock = threading.RLock()
        self.ring = ring
        self.sample_rate = sample_rate
        self.channels = channels
        self.bits = bits
        self._current: Optional[StreamSession] = None
        self._counter = 0
        self._sessions: dict[str, StreamSession] = {}
        self._live_connections: set = set()

    # ------------------------------------------------------------------ 会话
    @property
    def current(self) -> Optional[StreamSession]:
        with self._lock:
            return self._current

    def new_generation(self, kind: str, duration_ms: Optional[float] = None) -> StreamSession:
        """新建一代流（seek / 换曲 / 会话重建时调用）。自动作废旧连接。"""
        with self._lock:
            previous = self._current
            if previous is not None:
                previous.closed = True
            self._counter += 1
            token = f"{int(time.time())}-{self._counter}"
            content_type = (
                "audio/wav" if kind == "wav"
                else f"audio/L16;rate={self.sample_rate};channels={self.channels}"
            )
            session = StreamSession(
                token=token,
                generation=self.ring.generation,
                kind=kind,
                content_type=content_type,
                sample_rate=self.sample_rate,
                channels=self.channels,
                bits=self.bits,
                duration_ms=duration_ms,
            )
            if session.duration_ms and session.duration_ms > 0:
                session.total_bytes = int(
                    session.byte_rate * (session.duration_ms / 1000.0)
                )
            self._current = session
            self._sessions[token] = session
            # 只保留最近 8 个会话，避免长期运行内存泄漏
            if len(self._sessions) > 8:
                for stale in sorted(self._sessions, key=lambda t: self._sessions[t].created_at)[:-8]:
                    self._sessions.pop(stale, None)
            connections = list(self._live_connections)
        log.info(
            "新建实时流: path=/stream/%s.%s gen=%d duration=%s type=%s",
            token, session.extension, session.generation,
            f"{duration_ms:.0f}ms" if duration_ms else "未知", content_type,
        )
        # 在锁外关闭旧连接，避免与读线程互相等待
        for connection in connections:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass
        return session

    def update_duration(self, duration_ms: Optional[float]) -> None:
        """在渲染器开始拉流前修正声明时长。"""
        with self._lock:
            session = self._current
            if session is None or session.clients > 0:
                return
            session.duration_ms = duration_ms
            if duration_ms and duration_ms > 0:
                session.total_bytes = int(session.byte_rate * (duration_ms / 1000.0))
            else:
                session.total_bytes = None

    def get(self, token: str) -> Optional[StreamSession]:
        with self._lock:
            return self._sessions.get(token)

    def close_all(self) -> None:
        with self._lock:
            if self._current is not None:
                self._current.closed = True
            connections = list(self._live_connections)
        for connection in connections:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass

    def register_connection(self, connection) -> None:
        with self._lock:
            self._live_connections.add(connection)

    def unregister_connection(self, connection) -> None:
        with self._lock:
            self._live_connections.discard(connection)

    # ------------------------------------------------------------- 流式写出
    def serve(self, session: StreamSession, wfile, head_only: bool = False,
              range_start: int = 0, close_callback=None, idle_timeout: float = 20.0) -> None:
        """把 ``session`` 对应的 PCM 数据写入 ``wfile``（阻塞直到结束）。"""
        # HTTP Range / 重连属于**传输层细节**，不是用户 seek：同一个
        # generation/token 下渲染器可以任意次 GET / Range / reconnect，都不允许
        # 引起换代或 SetAVTransportURI（换代会让音箱重新缓冲并淡入）。此处只记录观测值。
        session.range_requests += 1
        session.last_range_info = "start=%d(%.1fs) head=%s req=%d" % (
            range_start, range_start / float(session.byte_rate or 1), head_only,
            session.range_requests)
        log.info(
            "渲染器开始拉流: token=%s gen=%d head=%s range_start=%s (第 %d 次连接)",
            session.token, session.generation, head_only, range_start, session.range_requests,
        )
        with self._lock:
            session.clients += 1
        if session.on_connect is not None:
            try:
                session.on_connect()
            except Exception:  # noqa: BLE001 - 诊断回调不得影响推流
                pass
        # 实时流没有可寻址的历史。渲染器经常用 Range 做「这个资源能不能拖」的探测
        # （真机日志里可见 bytes=0-、bytes=44-、bytes=1030600- 反复出现）。历史实现把
        # **文件字节偏移**直接当成**环形缓冲偏移**：
        #   * bytes=44-（跳过 WAV 头）会从 PCM 第 44 字节开始发，错位 11 帧；
        #   * bytes=1030600-（超过已产生的数据）会在 ring.read 上反复阻塞，
        #     而 HTTP 层已经声明了 200 + 完整 Content-Length，body 与声明不符，
        #     渲染器只能重试 → 就是真机日志里那种每几秒一次的重连抖动。
        # 因此这里**不再把 Range 当作定位依据**：一律从当前逻辑起点线性发送。
        # 这与 HTTP 层「200 + 完整长度 + Accept-Ranges: none」的声明保持一致，
        # 也是 AirConnect 在真实设备上验证过的做法。
        if range_start:
            log.info(
                "忽略 Range 偏移（实时流不可寻址，改从当前起点线性发送）: "
                "token=%s range_start=%s", session.token, range_start)

        # 暂停恢复（方案 A）：只在「逻辑 0 点」上做映射，且必须由控制器显式开启。
        mapped_offset: Optional[int] = None
        if session.recovery_byte_offset is not None:
            mapped_offset = session.recovery_byte_offset
            session.recovery_applied = True
            seconds = mapped_offset / float(session.byte_rate or 1)
            log.info(
                "暂停恢复映射生效: token=%s gen=%d 逻辑 Range 0 -> 实际环形偏移 %d 字节 (%.1fs)",
                session.token, session.generation, mapped_offset, seconds,
            )
        # 本连接已写出的 PCM 字节数。
        # **必须按连接独立计数**：历史实现用会话级 session.bytes_served 做流控，
        # 同一个 token 被多次 GET/重连（真机日志里一次恢复就有 5+ 次连接）后计数被
        # 叠加推过 total_bytes，之后所有连接都会立刻 break 并只补静音 —— 表现为
        # 「暂停/拖进度条后音响没声音」。session.bytes_served 仅保留作累计诊断。
        sent_conn = 0
        try:
            offset = mapped_offset if mapped_offset is not None else 0
            if session.kind == "wav" and not head_only:
                wfile.write(build_wav_header(session.total_bytes, session.sample_rate,
                                             session.channels, session.bits))
                wfile.flush()

            if head_only:
                return

            # 若渲染器中途重连并请求从 0 开始，从本代起点重新发送（实时流语义）
            chunk_size = 32 * 1024
            last_progress = time.monotonic()
            #: 连续输出下「真实 PCM 空窗」的起点；None 表示当前有真实 PCM
            silence_since: Optional[float] = None
            silence_notified = False
            read_timeout = (session.read_timeout_s if session.continuous_output else 1.0)
            while True:
                if session.closed:
                    break
                # 暂停 keepalive：输出层生成静音（不读环形缓冲、不污染 AirPlay 时间线）。
                # 渲染器因此一直有数据可拉、保持 PLAYING，恢复时无需任何 UPnP 操作。
                if session.silence_mode:
                    if (session.total_bytes is not None
                            and sent_conn >= session.total_bytes):
                        break
                    silence = b"\x00" * chunk_size
                    wfile.write(silence)
                    try:
                        wfile.flush()
                    except Exception:  # noqa: BLE001
                        break
                    sent_conn += len(silence)
                    session.bytes_served += len(silence)
                    session.silence_bytes += len(silence)
                    session.last_activity = time.monotonic()
                    silence_since = None
                    if session.on_bytes is not None:
                        try:
                            session.on_bytes(session.bytes_served)
                        except Exception:  # noqa: BLE001
                            pass
                    # 按实时速率节流（44.1k/16bit/2ch 时 32KB ≈ 0.19s）
                    time.sleep(chunk_size / float(session.byte_rate or 176400))
                    continue
                if session.total_bytes is not None and sent_conn >= session.total_bytes:
                    break
                try:
                    data, offset = self.ring.read(offset, session.generation, chunk_size,
                                                  read_timeout)
                except StaleGeneration:
                    log.info("流已换届，主动断开旧连接: token=%s", session.token)
                    break
                except BufferOverflow:
                    log.warning("渲染器消费过慢，缓冲区被覆盖，断开连接: token=%s", session.token)
                    break
                if not data:
                    if session.continuous_output:
                        # 连续媒体输出（ARCHITECTURE_V2 第 6/7 节）：DLNA 仍在播放，
                        # 但暂时没有真实 AirPlay PCM（恢复/seek/切歌的空窗）→ 用静音
                        # 填充真实时间轴，**绝不主动 EOF/断开**。静音只在输出层生成，
                        # 绝不会写入 AirPlay 环形缓冲或时间线。
                        now = time.monotonic()
                        if silence_since is None:
                            silence_since = now
                            silence_notified = False
                            log.info("连续输出：真实 PCM 暂时不足，切换为静音填充"
                                     "（token=%s gen=%d）", session.token, session.generation)
                        silence = b"\x00" * chunk_size
                        wfile.write(silence)
                        try:
                            wfile.flush()
                        except Exception:  # noqa: BLE001
                            break
                        sent_conn += len(silence)
                        session.bytes_served += len(silence)
                        session.silence_bytes += len(silence)
                        session.last_activity = now
                        if session.on_bytes is not None:
                            try:
                                session.on_bytes(session.bytes_served)
                            except Exception:  # noqa: BLE001
                                pass
                        # 静音超时：只通知控制层进入 RECOVERING，不在此处做任何网络/
                        # 状态操作，也**不**断开响应（第 8 节）。
                        if (not silence_notified
                                and session.silence_timeout_s > 0
                                and now - silence_since >= session.silence_timeout_s):
                            silence_notified = True
                            log.warning(
                                "连续输出：真实 PCM 已中断 %.1fs（阈值 %.1fs），"
                                "通知控制层进入恢复流程（不断开 HTTP）: token=%s",
                                now - silence_since, session.silence_timeout_s,
                                session.token)
                            if session.on_silence_timeout is not None:
                                try:
                                    session.on_silence_timeout()
                                except Exception:  # noqa: BLE001
                                    log.exception("静音超时回调异常")
                        time.sleep(chunk_size / float(session.byte_rate or 176400))
                        continue
                    if time.monotonic() - last_progress > idle_timeout:
                        log.warning("音频流超时（%.0fs 无数据），断开连接: token=%s",
                                    idle_timeout, session.token)
                        break
                    continue
                last_progress = time.monotonic()
                if silence_since is not None:
                    log.info("连续输出：真实 PCM 恢复，结束静音填充（空窗 %.1fs，"
                             "token=%s gen=%d）", last_progress - silence_since,
                             session.token, session.generation)
                    silence_since = None
                    silence_notified = False
                if session.total_bytes is not None:
                    remaining = session.total_bytes - sent_conn
                    if len(data) > remaining:
                        data = data[:remaining]
                wfile.write(data)
                sent_conn += len(data)
                session.bytes_served += len(data)
                session.last_activity = time.monotonic()
                if session.on_bytes is not None:
                    try:
                        session.on_bytes(session.bytes_served)
                    except Exception:  # noqa: BLE001 - 诊断回调不得影响推流
                        pass

            # 声明了总长度但本连接发送不足：补静音，避免渲染器等到超时
            if (session.total_bytes is not None
                    and sent_conn < session.total_bytes
                    and not session.closed):
                _pad_silence(wfile, session.total_bytes - sent_conn)
            try:
                wfile.flush()
            except Exception:  # noqa: BLE001
                pass
        except (BrokenPipeError, ConnectionResetError):
            log.debug("渲染器提前断开连接: token=%s", session.token)
        except OSError as exc:
            log.debug("流写出失败（%s）: token=%s", exc, session.token)
        finally:
            with self._lock:
                session.clients = max(0, session.clients - 1)
            if close_callback is not None:
                try:
                    close_callback()
                except Exception:  # noqa: BLE001
                    pass
            log.info(
                "渲染器拉流结束: token=%s 本次连接 %.1fs（本 token 累计 %.1fs，共 %d 次连接）",
                session.token,
                sent_conn / float(session.byte_rate or 1),
                session.bytes_served / float(session.byte_rate or 1),
                session.range_requests,
            )


def _pad_silence(wfile, num_bytes: int) -> None:
    silence = b"\x00" * min(num_bytes, 65536)
    written = 0
    while written < num_bytes:
        count = min(len(silence), num_bytes - written)
        try:
            wfile.write(silence[:count])
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        written += count
