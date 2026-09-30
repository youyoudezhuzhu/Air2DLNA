"""Virtual Player 架构单元测试（1.0.23 / ARCHITECTURE_V2）。

覆盖本轮重构新增/变更的行为，全部**自包含且封闭**：

* 状态机：真实状态（IDLE/BUFFERING/PLAYING/PAUSED/SEEKING/TRACK_SWITCHING/
  RECOVERING/STOPPING）与独立的请求状态（*_REQUESTED）严格分离；
* 连续输出：真实 PCM ↔ 静音切换、绝不 EOF、静音不污染 AirPlay RingBuffer；
* 静音超时：只通知控制层进入 RECOVERING，不断开 HTTP；
* 反向控制：逐项能力检测（SUPPORTED/UNSUPPORTED/UNKNOWN）+ 不伪造状态
  （用 127.0.0.1 上的本地假 DACP 服务器验证真实 HTTP 请求与响应）；
* 控制回环防护：DLNA 发起、被 AirPlay 回显的状态变化不得再次触发 DLNA 动作；
* Renderer Profile：xiaomi_s12 / generic 的字段取值与覆盖机制（S12 的取值来自
  真机日志实测：Pause 后自行 STOPPED、不保持 HTTP、恢复必须重新宣告）。
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "app" / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from air2dlna import state as state_mod  # noqa: E402
from air2dlna.airplay_remote import SUPPORTED, UNKNOWN, UNSUPPORTED  # noqa: E402
from air2dlna.dlna_output import MODE_PAUSE, MODE_PLAY  # noqa: E402
from air2dlna.metadata import MetadataItem  # noqa: E402
from air2dlna.renderer_profile import profile_for, select_profile  # noqa: E402
from air2dlna.ringbuffer import PcmRingBuffer  # noqa: E402
from air2dlna.stream import StreamManager  # noqa: E402
from air2dlna.timeline import AudioTimeline  # noqa: E402
from air2dlna.virtual_player import (  # noqa: E402
    BUFFERING,
    IDLE,
    PAUSED,
    PAUSE_REQUESTED,
    PLAYING,
    RECOVERING,
    SEEKING,
    VirtualPlayer,
)

SAMPLE_RATE = 44100
CHANNELS = 2
BYTE_RATE = SAMPLE_RATE * CHANNELS * 2


class _Cfg(dict):
    def get(self, key, default=None):  # noqa: D102
        return dict.get(self, key, default)

    def update(self, changes):  # noqa: D102
        dict.update(self, changes)


class _StubClient:
    """最小 UPnP 客户端替身（记录调用，便于断言 DLNA 动作）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.volume = 0

    def get_transport_info(self):  # noqa: D102
        return {"state": "PLAYING"}

    def get_position_info(self):  # noqa: D102
        return {"rel_time_ms": 1000.0, "duration_ms": 226000.0}

    def set_av_transport_uri(self, *a, **kw):  # noqa: D102
        self.calls.append("set_uri")
        return True

    def play(self, *a, **kw):  # noqa: D102
        self.calls.append("play")
        return True

    def pause(self, *a, **kw):  # noqa: D102
        self.calls.append("pause")
        return True

    def stop(self, *a, **kw):  # noqa: D102
        self.calls.append("stop")
        return True

    def get_volume(self):  # noqa: D102
        return self.volume

    def set_volume(self, volume):  # noqa: D102
        self.calls.append("set_volume")
        self.volume = int(volume)
        return True

    def subscribe(self, callback, timeout_s=1800):  # noqa: D102
        return "uuid:sub-1"

    def renew_subscription(self, sid, timeout_s=1800):  # noqa: D102
        return True


class _Record:
    def __init__(self, model: str = "S12", name: str = "小爱音箱-测试",
                 udn: str = "uuid:fake") -> None:
        self.udn = udn
        self.name = name
        self.ip = "192.168.1.60"
        self.client = _StubClient()
        self.model = model
        self.manufacturer = "Mi, Inc." if model == "S12" else ""
        self.online = True
        self.supported_mime = "http-get:*:audio/wav:*"
        self.capability_error = ""


class _Registry:
    def __init__(self, record) -> None:
        self._record = record

    def selected(self):  # noqa: D102
        return self._record

    @staticmethod
    def resolve_stream_kind(record, output_format):  # noqa: D102
        return "audio/wav", "wav"


class _Session:
    def __init__(self, token, generation, **kw) -> None:
        self.token = token
        self.generation = generation
        self.closed = False
        self.clients = kw.get("clients", 1)
        self.created_at = time.monotonic()
        self.last_activity = time.monotonic()
        self.duration_ms = None
        self.content_type = "audio/wav"
        self.protocol_info = "http-get:*:audio/wav:*"
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self.bits = 16
        self.bytes_served = 0
        self.range_requests = 0
        self.last_range_info = ""
        self.silence_mode = False
        self.continuous_output = False
        self.silence_timeout_s = 0.0
        self.on_silence_timeout = None
        self.silence_bytes = 0
        self.read_timeout_s = 0.2
        self.on_bytes = None
        self.on_connect = None
        self.recovery_byte_offset = None
        self.recovery_applied = False

    def path(self, prefix="/stream"):  # noqa: D102
        return f"{prefix}/{self.token}.wav"


class _Streams:
    def __init__(self, ring) -> None:
        self.ring = ring
        self.created: list[str] = []
        self._sessions: dict[str, _Session] = {}

    def new_generation(self, kind, duration_ms=None):  # noqa: D102
        token = f"tok-{len(self.created) + 1}"
        session = _Session(token, self.ring.generation)
        self.created.append(token)
        self._sessions[token] = session
        return session

    def get(self, token):  # noqa: D102
        return self._sessions.get(token)

    def update_duration(self, duration_ms):  # noqa: D102
        return None

    def close_all(self):  # noqa: D102
        return None


def build_controller(model: str = "S12", udn: str = "uuid:fake"):
    """构造一个带假渲染器 / 假流管理器的 BridgeController。"""
    config = _Cfg({
        "preroll_seconds": 0.0,
        "output_format": "auto",
        "http_port": 8788,
        "buffer_seconds": 120,
        "av_offset_ms": 0,
        "metadata_poll_seconds": 3.0,
        "recovery_mode": "current",
    })
    ring = PcmRingBuffer(BYTE_RATE * 30, sample_rate=SAMPLE_RATE, channels=CHANNELS)
    timeline = AudioTimeline(sample_rate=SAMPLE_RATE)
    streams = _Streams(ring)
    record = _Record(model=model, udn=udn)
    controller = state_mod.BridgeController(
        config, registry=_Registry(record), ring=ring, timeline=timeline, streams=streams)
    controller.timeline.on_play_begin()
    return controller, config, ring, timeline, record, streams


def start_playing(controller) -> None:
    """让 Virtual Player 进入真正的 PLAYING（pbeg + 真实下发 DLNA Play）。"""
    record = controller.registry.selected()
    controller._handle_play(False)
    controller._do_play(record, record.client, controller._gen_token)


# ------------------------------------------------------------------ 假 DACP 服务
class _DacpHandler(BaseHTTPRequestHandler):
    """本地假 DACP 服务：记录请求并返回预设状态码。"""

    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        server = self.server
        server.requests.append((self.path, dict(self.headers)))  # type: ignore[attr-defined]
        body = b""
        self.send_response(server.response_status)  # type: ignore[attr-defined]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: D102
        pass


class DacpServerFixture:
    """127.0.0.1 上的假 DACP 服务器（封闭、无外部网络）。"""

    def __init__(self, status: int = 200) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _DacpHandler)
        self.server.daemon_threads = True
        self.server.requests = []          # type: ignore[attr-defined]
        self.server.response_status = status  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def requests(self):
        return self.server.requests  # type: ignore[attr-defined]

    def close(self) -> None:
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:  # noqa: BLE001
            pass


def give_dacp_credentials(controller, port: int, dacp_id: str = "DACPID",
                          active_remote: str = "TOKEN", ip: str = "127.0.0.1") -> None:
    """通过真实元数据事件把 DACP 凭据灌入（走 _handle_ssnc → 凭据捕获）。"""
    for code, payload in (("conn", ip), ("daid", dacp_id), ("acre", active_remote),
                          ("dapo", str(port))):
        data = payload.encode()
        controller.on_metadata_item(
            MetadataItem(type="ssnc", code=code, data=data, length=len(data)))


# ------------------------------------------------------------------------ 状态机
class StateMachineTests(unittest.TestCase):
    def test_machine_states_transition_for_real_events(self) -> None:
        controller, _, _, _, record, _ = build_controller()
        self.assertEqual(IDLE, controller.state.machine_state, "初始应为 IDLE")

        controller._handle_play(False)                    # pbeg
        self.assertEqual(BUFFERING, controller.state.machine_state)

        controller._do_play(record, record.client, controller._gen_token)
        self.assertEqual(PLAYING, controller.state.machine_state)
        self.assertEqual(PLAYING, controller.state.state)

        controller._handle_pause()                        # paus
        self.assertEqual(PAUSED, controller.state.machine_state)
        self.assertEqual(PAUSED, controller.state.state)

        controller._handle_flush("12345")                 # 真实 seek
        self.assertEqual(SEEKING, controller.state.machine_state,
                         "seek 期间应处于 SEEKING（不是 PAUSED/PLAYING）")
        self.assertEqual(BUFFERING, controller.state.state,
                         "对外 legacy 视图在 seek 期间显示 BUFFERING")

        controller._handle_play_stream_end("pend")
        controller._handle_session_end("aend")
        self.assertEqual(IDLE, controller.state.machine_state)
        self.assertEqual("STOPPED", controller.state.state)

    def test_stopping_is_distinct_from_idle(self) -> None:
        controller, _, _, _, _, _ = build_controller()
        start_playing(controller)
        controller._enter("STOPPING", reason="test")
        self.assertEqual("STOPPING", controller.state.machine_state)
        self.assertEqual("STOPPED", controller.state.state)

    def test_recovering_state_used_by_stall_rebuild(self) -> None:
        controller, _, ring, _, _, streams = build_controller()
        start_playing(controller)
        controller._renderer_token = controller._gen_token
        session = streams.get(controller._gen_token)
        session.clients = 0
        session.last_activity = time.monotonic() - 30.0
        controller._last_stall_rebuild = 0.0

        controller._check_renderer_stream()

        self.assertEqual("RECOVERING", controller.state.machine_state,
                         "链路真的断了才允许进入 RECOVERING 并重建")

    def test_playback_state_exposes_new_fields(self) -> None:
        controller, _, _, _, _, _ = build_controller()
        payload = controller.state.to_dict()
        for key in ("state", "machine_state", "pending_request", "control_source",
                    "request_id"):
            self.assertIn(key, payload)
        self.assertNotEqual(payload["state"], payload["machine_state"],
                            "legacy 状态与机器状态是两个概念，不能混为一谈")


# ------------------------------------------------------------------ 连续输出层
class ContinuousOutputTests(unittest.TestCase):
    """静音只在输出层生成；DLNA 播放期间绝不 EOF。"""

    def _serving(self, manager, session):
        out = _FakeWFile()
        done = threading.Event()

        def run():
            manager.serve(session, out, idle_timeout=0.5)
            done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return out, done

    def test_no_pcm_emits_silence_and_never_eofs(self) -> None:
        ring = PcmRingBuffer(BYTE_RATE * 5, sample_rate=SAMPLE_RATE, channels=CHANNELS)
        manager = StreamManager(ring)
        ring.flush()
        session = manager.new_generation("l16")
        session.continuous_output = True
        session.read_timeout_s = 0.05
        session.silence_timeout_s = 0.0
        out, done = self._serving(manager, session)

        time.sleep(0.3)

        self.assertFalse(done.is_set(), "真实 PCM 缺失期间绝不 EOF / 断开响应")
        self.assertGreater(len(out.data), 0)
        self.assertEqual(b"\x00", bytes(out.data[:1]), "应输出静音 PCM")
        self.assertEqual(0, ring.write_offset,
                         "静音绝不能被写回 AirPlay 环形缓冲")

        session.closed = True
        self.assertTrue(done.wait(2.0), "会话关闭后连接才结束")

    def test_real_pcm_resumes_and_ends_silence(self) -> None:
        ring = PcmRingBuffer(BYTE_RATE * 5, sample_rate=SAMPLE_RATE, channels=CHANNELS)
        manager = StreamManager(ring)
        ring.flush()
        session = manager.new_generation("l16")
        session.continuous_output = True
        session.read_timeout_s = 0.05
        out, done = self._serving(manager, session)
        time.sleep(0.25)
        silence_len = len(out.data)
        self.assertGreater(silence_len, 0)

        pattern = b"\x7f" * (BYTE_RATE // 4)
        ring.append(pattern)
        deadline = time.monotonic() + 2.0
        switched = False
        while time.monotonic() < deadline:
            if b"\x7f" in bytes(out.data[silence_len:]):
                switched = True
                break
            time.sleep(0.02)

        self.assertTrue(switched, "真实 PCM 到达后应切回真实音频")
        self.assertFalse(done.is_set())
        self.assertEqual(len(pattern), ring.write_offset,
                         "环形缓冲只包含真实 PCM，未被静音污染")
        session.closed = True
        done.wait(2.0)

    def test_silence_timeout_notifies_without_eof(self) -> None:
        ring = PcmRingBuffer(BYTE_RATE * 5, sample_rate=SAMPLE_RATE, channels=CHANNELS)
        manager = StreamManager(ring)
        ring.flush()
        session = manager.new_generation("l16")
        session.continuous_output = True
        session.read_timeout_s = 0.02
        session.silence_timeout_s = 0.1
        hits: list[float] = []
        session.on_silence_timeout = lambda: hits.append(time.monotonic())
        out, done = self._serving(manager, session)

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not hits:
            time.sleep(0.02)

        self.assertTrue(hits, "静音超时应通知控制层（进入 RECOVERING）")
        self.assertFalse(done.is_set(), "静音超时本身不得断开 HTTP 响应")
        session.closed = True
        done.wait(2.0)

    def test_timeout_taken_once_by_controller(self) -> None:
        controller, _, _, _, _, _ = build_controller()
        start_playing(controller)
        controller.output.note_silence_timeout()
        self.assertTrue(controller.output.take_silence_timeout())
        self.assertFalse(controller.output.take_silence_timeout(), "标志只能取一次")


class _FakeWFile:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, chunk: bytes) -> int:
        self.data.extend(chunk)
        return len(chunk)

    def flush(self) -> None:
        pass


# ------------------------------------------------------------------ 反向控制
class ReverseControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.controller, self.config, self.ring, self.timeline, self.record, _ = \
            build_controller()
        start_playing(self.controller)

    def test_no_credentials_reports_unknown_and_never_fakes(self) -> None:
        controller = self.controller
        self.assertEqual(UNKNOWN, controller.remote.capability("play"))
        self.assertEqual(UNKNOWN, controller.remote.capability("next"))
        for name in ("play", "pause", "seek", "next", "previous"):
            self.assertEqual(UNKNOWN, controller.remote.capability(name))

        result = controller.request_control("pause", source="DLNA")

        self.assertFalse(result["ok"])
        self.assertEqual(UNKNOWN, result["capability"])
        self.assertFalse(result["state_changed"])
        self.assertEqual(PLAYING, controller.state.state, "不得假装状态改变")
        self.assertEqual("", controller.state.pending_request)

    def test_credentials_captured_from_metadata_pipe(self) -> None:
        server = DacpServerFixture(200)
        try:
            give_dacp_credentials(self.controller, server.port)
        finally:
            server.close()
        creds = self.controller.remote.credentials
        self.assertTrue(creds.complete())
        self.assertEqual("DACPID", creds.dacp_id)
        self.assertEqual("TOKEN", creds.active_remote)
        self.assertEqual("127.0.0.1", creds.sender_ip)

    def test_supported_pause_issues_dacp_and_waits_for_airplay(self) -> None:
        server = DacpServerFixture(200)
        try:
            give_dacp_credentials(self.controller, server.port)

            result = self.controller.request_control(
                "pause", source="DLNA", request_id="req-1")

            self.assertTrue(result["ok"])
            self.assertTrue(result["awaiting_confirmation"])
            self.assertFalse(result["state_changed"])
            self.assertEqual(PLAYING, self.controller.state.state,
                             "DACP 调用成功 ≠ AirPlay 已暂停，状态必须等确认")
            self.assertEqual(PAUSE_REQUESTED, self.controller.state.pending_request)

            path, raw_headers = server.requests[-1]
            headers = {k.lower(): v for k, v in raw_headers.items()}
            self.assertEqual("/ctrl-int/1/pause", path)
            self.assertEqual("TOKEN", headers.get("active-remote"))
            self.assertEqual("DACPID", headers.get("dacp-id"))

            self.controller._handle_pause()               # AirPlay 确认
            self.assertEqual(PAUSED, self.controller.state.state)
            self.assertEqual("", self.controller.state.pending_request)
            self.assertEqual(SUPPORTED, self.controller.remote.capability("pause"))
        finally:
            server.close()

    def test_unsupported_capability_keeps_state_and_track(self) -> None:
        server = DacpServerFixture(501)
        try:
            give_dacp_credentials(self.controller, server.port)
            self.controller.state.title = "Track A"
            self.controller.state.artist = "Artist A"
            before = len(server.requests)

            result = self.controller.request_control("next", source="DLNA")

            self.assertFalse(result["ok"], "501 = 明确不支持，不得视为成功")
            self.assertEqual(UNSUPPORTED, result["capability"])
            self.assertEqual(PLAYING, self.controller.state.state)
            self.assertEqual("Track A", self.controller.state.title,
                             "不得自己猜下一首 / 不得改变曲目")
            self.assertEqual(before + 1, len(server.requests))

            # 已判定 UNSUPPORTED → 不再重试（也不伪造成功）
            self.controller.request_control("next", source="DLNA")
            self.assertEqual(before + 1, len(server.requests),
                             "UNSUPPORTED 的能力不应反复发请求")
        finally:
            server.close()

    def test_failed_call_does_not_change_state(self) -> None:
        server = DacpServerFixture(500)                    # 无法据此判定能力
        try:
            give_dacp_credentials(self.controller, server.port)
            result = self.controller.request_control("previous", source="DLNA")
            self.assertFalse(result["ok"])
            self.assertEqual(UNKNOWN, result["capability"],
                             "500 不能得出结论，能力保持 UNKNOWN")
            self.assertEqual(PLAYING, self.controller.state.state)
            self.assertEqual("", self.controller.state.pending_request)
        finally:
            server.close()

    def test_seek_capability_path_and_parameters(self) -> None:
        server = DacpServerFixture(200)
        try:
            give_dacp_credentials(self.controller, server.port)
            result = self.controller.request_control(
                "seek", source="DLNA", position_ms=90000)
            self.assertTrue(result["ok"])
            path, _headers = server.requests[-1]
            self.assertEqual("/ctrl-int/1/setproperty?dacp.playingtime=90000", path)
            self.assertEqual(PLAYING, self.controller.state.state)
            pending = self.controller.pending_request()
            self.assertEqual("seek", pending["action"])
            self.assertEqual("DLNA", pending["source"])

            self.controller._handle_flush("3969000")       # AirPlay 确认 seek
            self.assertEqual("", self.controller.state.pending_request)
        finally:
            server.close()

    def test_reverse_control_can_be_disabled_by_config(self) -> None:
        server = DacpServerFixture(200)
        try:
            give_dacp_credentials(self.controller, server.port)
            self.config["reverse_control_enabled"] = False
            result = self.controller.request_control("play", source="DLNA")
            self.assertFalse(result["ok"])
            self.assertEqual([], server.requests, "关闭后不得发出任何 DACP 请求")
        finally:
            server.close()


# -------------------------------------------------------------- 控制回环防护
class ControlLoopPreventionTests(unittest.TestCase):
    def test_dlna_pause_echo_does_not_reissue_dlna_pause(self) -> None:
        server = DacpServerFixture(200)
        try:
            controller, _, _, _, record, _ = build_controller()
            start_playing(controller)
            give_dacp_credentials(controller, server.port)
            # 收敛线程尚未运行时，意图停在最近一次 PLAY
            self.assertEqual(MODE_PLAY, controller.output.intent_mode)

            result = controller.request_control("pause", source="DLNA", request_id="r1")
            self.assertTrue(result["ok"])

            controller._handle_pause()                    # AirPlay 回显这次暂停

            self.assertEqual(PAUSED, controller.state.state, "实际状态应更新")
            self.assertEqual(MODE_PLAY, controller.output.intent_mode,
                             "DLNA 发起的暂停被回显后，不得再次向渲染器下发 Pause")
            self.assertEqual([], [c for c in record.client.calls if c == "pause"])
        finally:
            server.close()

    def test_airplay_originated_pause_still_controls_dlna(self) -> None:
        controller, _, _, _, _, _ = build_controller()
        start_playing(controller)

        controller._handle_pause()

        self.assertEqual(PAUSED, controller.state.state)
        self.assertEqual(MODE_PAUSE, controller.output.intent_mode,
                         "AirPlay 自己的暂停必须同步给 DLNA 渲染器")

    def test_request_id_and_source_are_recorded(self) -> None:
        server = DacpServerFixture(200)
        try:
            controller, _, _, _, _, _ = build_controller()
            start_playing(controller)
            give_dacp_credentials(controller, server.port)

            controller.request_control("pause", source="DLNA", request_id="abc-123")

            self.assertEqual("abc-123", controller.state.request_id)
            self.assertEqual("DLNA", controller.state.control_source)
            self.assertEqual(PAUSE_REQUESTED, controller.state.pending_request)
            self.assertEqual(PLAYING, controller.state.state,
                             "请求状态与播放状态必须分离")
        finally:
            server.close()


# ---------------------------------------------------------------- Renderer Profile
class RendererProfileTests(unittest.TestCase):
    def test_s12_profile_matches_real_device_evidence(self) -> None:
        record = _Record(model="S12", name="小爱音箱", udn="uuid:s12")
        profile = select_profile(record)

        self.assertEqual("xiaomi_s12", profile.name)
        self.assertFalse(profile.supports_pause,
                         "实测 Pause 后自行 STOPPED → 不会停留在 PAUSED_PLAYBACK")
        self.assertFalse(profile.pause_keeps_uri)
        self.assertFalse(profile.pause_keeps_http, "暂停会连带丢弃 HTTP 拉流")
        self.assertTrue(profile.resume_requires_reannounce,
                        "已 STOPPED，恢复必须重新 SetAVTransportURI + Play")
        self.assertEqual("none", profile.reconnect_behavior)

    def test_generic_profile_is_conservative(self) -> None:
        record = _Record(model="FakeRenderer-1", name="Fake", udn="uuid:fake")
        profile = select_profile(record)

        self.assertEqual("generic", profile.name)
        self.assertTrue(profile.supports_pause, "通用设备先尝试真正的 Pause")
        self.assertTrue(profile.pause_keeps_uri)
        self.assertTrue(profile.pause_keeps_http)
        self.assertFalse(profile.resume_requires_reannounce)

    def test_buffer_bounds_from_spec(self) -> None:
        record = _Record(model="FakeRenderer-1", udn="uuid:f2")
        profile = select_profile(record)
        self.assertGreaterEqual(profile.minimum_buffer_ms, 300)
        self.assertLessEqual(profile.minimum_buffer_ms, 500)
        self.assertAlmostEqual(1000, profile.target_buffer_ms, delta=1)
        self.assertGreaterEqual(profile.maximum_buffer_ms, 1500)
        self.assertLessEqual(profile.maximum_buffer_ms, 2000)

    def test_config_override_wins(self) -> None:
        profile = profile_for(model="S12", name="小爱音箱", override="generic")
        self.assertEqual("generic", profile.name)

    def test_buffer_config_overrides_profile_defaults(self) -> None:
        record = _Record(model="S12", udn="uuid:s12b")
        config = _Cfg({"target_buffer_ms": 700, "minimum_buffer_ms": 200})
        profile = select_profile(record, config=config)
        self.assertEqual(700, profile.target_buffer_ms)
        self.assertEqual(200, profile.minimum_buffer_ms)

    def test_dlna_output_uses_profile_for_pause_strategy(self) -> None:
        """S12 workaround 在 DLNA Output 层由 Profile 驱动，不在 Virtual Player 核心。"""
        controller, _, _, _, record, _ = build_controller(model="S12")
        controller.state.renderer_state = "PLAYING"      # 不在 STOPPED 才会真正下发
        controller.output.do_pause(record, record.client)
        self.assertEqual(["stop"], record.client.calls,
                         "xiaomi_s12: supports_pause=False → 直接使用 Stop")

        controller2, _, _, _, record2, _ = build_controller(
            model="FakeRenderer-1", udn="uuid:generic")
        controller2.state.renderer_state = "PLAYING"
        controller2.output.do_pause(record2, record2.client)
        self.assertEqual(["pause"], record2.client.calls,
                         "generic: 先尝试真正的 AVTransport#Pause")

    def test_status_exposes_profile_and_reverse_control(self) -> None:
        controller, _, _, _, _, _ = build_controller()
        status = controller.status()
        self.assertIn("renderer_profile", status)
        self.assertEqual("xiaomi_s12", status["renderer_profile"]["name"])
        self.assertIn("reverse_control", status)
        self.assertIn("capabilities", status["reverse_control"])
        self.assertIn("virtual_player", status)
        self.assertIn("machine_state", status["virtual_player"])
        for key in ("generation", "renderer_rel_time_ms", "rel_offset_ms", "rel_rate",
                    "uri_count", "uri_log", "last_rebuild_reason", "awaiting_new_stream"):
            self.assertIn(key, status["diagnostics"])


# ----------------------------------------------------------- 静音超时 → RECOVERING
class SilenceTimeoutRecoveryTests(unittest.TestCase):
    def test_silence_timeout_enters_recovering_and_keeps_output(self) -> None:
        controller, _, ring, _, _, streams = build_controller()
        start_playing(controller)
        session = streams.get(controller._gen_token)
        session.clients = 1                       # 渲染器仍在拉流（静音保持连接）
        controller.state.renderer_state = "PLAYING"
        generation_before = ring.generation

        controller.output.note_silence_timeout()
        controller._check_silence_timeout()

        self.assertEqual(RECOVERING, controller.state.machine_state)
        self.assertEqual(generation_before, ring.generation,
                         "渲染器仍在拉流时不得重建（静音保持连续输出）")

    def test_silence_timeout_without_clients_rebuilds_as_fallback(self) -> None:
        controller, _, ring, _, _, streams = build_controller()
        start_playing(controller)
        session = streams.get(controller._gen_token)
        session.clients = 0
        controller.state.renderer_state = "STOPPED"
        controller._last_stall_rebuild = 0.0
        generation_before = ring.generation

        controller.output.note_silence_timeout()
        controller._check_silence_timeout()

        self.assertEqual(generation_before + 1, ring.generation,
                         "链路确认不可用时才执行 recovery 原语")


# ------------------------------------------------------------------ VirtualPlayer 隔离
class ModuleBoundaryTests(unittest.TestCase):
    def test_virtual_player_does_not_touch_upnp_directly(self) -> None:
        """状态协调器不得直接发 SOAP：所有 DLNA 动作都在 DLNAOutput 里。"""
        import inspect

        source = inspect.getsource(VirtualPlayer)
        for forbidden in ("set_av_transport_uri(", "client.play(", "client.pause(",
                          "client.stop(", "urlopen("):
            self.assertNotIn(forbidden, source,
                             f"Virtual Player 不应直接调用 {forbidden}")

    def test_no_silence_written_into_ring_buffer_by_player(self) -> None:
        """第 23/28 节：Virtual Player 绝不把静音写进 AirPlay RingBuffer。

        （``on_audio_bytes`` 追加的是**真实 AirPlay PCM**，那是它的职责；
        这里只禁止追加任何合成数据。）
        """
        import inspect

        source = inspect.getsource(VirtualPlayer)
        for forbidden in ('ring.append(b"\\x00"', "ring.append(bytes(",
                          "ring.append(b'\\x00'", "ring.append(b\"\\x00\" *"):
            self.assertNotIn(forbidden, source,
                             "Virtual Player 不得向环形缓冲写入任何合成静音")
        self.assertIn("self.ring.append(data)", source,
                      "只允许追加来自 AirPlay 的真实 PCM")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
