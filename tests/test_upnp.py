"""UPnP 模块单元测试（纯函数 + 本地 127.0.0.1 HTTP 服务器）。

所有服务器仅绑定 127.0.0.1 的随机端口，测试不使用外部网络，也不做长时间等待。
"""

from __future__ import annotations

import http.server
import socket
import sys
import threading
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

# 基于 __file__ 稳健地把 app/server 加入 sys.path，使 `airplay2dlna` 可导入。
REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "app" / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from airplay2dlna import upnp  # noqa: E402

SOAP_ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"

# ---------------------------------------------------------------------------
# 测试夹具：本地 SOAP/GENA/GET 服务器
# ---------------------------------------------------------------------------


class _TestServer(http.server.ThreadingHTTPServer):
    """记录请求并可按测试配置返回内容的 HTTP 服务器。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests: list[dict] = []
        self.responder = None  # callable(body: bytes) -> (status: int, payload: bytes)
        self.get_body: bytes = b""
        self.get_status: int = 200
        self.sid: str = ""


class _Handler(http.server.BaseHTTPRequestHandler):
    """处理 POST(SOAP) / SUBSCRIBE / UNSUBSCRIBE / GET 的测试处理器。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: D102 - 静默测试日志
        pass

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def _send(self, status: int, payload: bytes, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        body = self._read_body()
        self.server.requests.append(
            {"method": "POST", "path": self.path, "headers": dict(self.headers.items()), "body": body}
        )
        if self.server.responder is not None:
            status, payload = self.server.responder(body)
        else:
            status, payload = 200, b""
        self._send(status, payload)

    def do_SUBSCRIBE(self) -> None:  # noqa: N802
        self._read_body()
        self.server.requests.append(
            {"method": "SUBSCRIBE", "path": self.path, "headers": dict(self.headers.items()), "body": b""}
        )
        headers = {"SID": self.server.sid} if self.server.sid else {}
        self._send(200, b"", headers)

    def do_UNSUBSCRIBE(self) -> None:  # noqa: N802
        self._read_body()
        self.server.requests.append(
            {"method": "UNSUBSCRIBE", "path": self.path, "headers": dict(self.headers.items()), "body": b""}
        )
        self._send(200, b"")

    def do_GET(self) -> None:  # noqa: N802
        self.server.requests.append(
            {"method": "GET", "path": self.path, "headers": dict(self.headers.items()), "body": b""}
        )
        self._send(self.server.get_status, self.server.get_body)


SUCCESS_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
            s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:GetTransportInfoResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">
      <CurrentTransportState>PLAYING</CurrentTransportState>
      <CurrentTransportStatus>OK</CurrentTransportStatus>
      <CurrentSpeed>1</CurrentSpeed>
    </u:GetTransportInfoResponse>
  </s:Body>
</s:Envelope>
"""

FAULT_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Body>
    <s:Fault>
      <faultcode>s:Client</faultcode>
      <faultstring>UPnPError</faultstring>
      <detail>
        <UPnPError xmlns="urn:schemas-upnp-org:control-1-0">
          <errorCode>402</errorCode>
          <errorDescription>Invalid Args</errorDescription>
        </UPnPError>
      </detail>
    </s:Fault>
  </s:Body>
</s:Envelope>
"""

FAULT_STRING_ONLY_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Body>
    <s:Fault>
      <faultcode>s:Server</faultcode>
      <faultstring>Something went wrong</faultstring>
    </s:Fault>
  </s:Body>
</s:Envelope>
"""

DEVICE_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>Network Speaker</friendlyName>
    <manufacturer>Acme</manufacturer>
    <modelName>Speaker 100</modelName>
    <UDN>uuid:dddddddd-0000-0000-0000-000000000000</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
        <controlURL>AVTransport/control</controlURL>
        <eventSubURL>AVTransport/event</eventSubURL>
        <SCPDURL>AVTransport/scpd.xml</SCPDURL>
      </service>
    </serviceList>
  </device>
</root>
"""


class UpnpTimeTests(unittest.TestCase):
    """parse_upnp_time / format_upnp_time。"""

    def test_parse_typical_values(self) -> None:
        self.assertEqual(upnp.parse_upnp_time("0:00:00"), 0)
        self.assertEqual(upnp.parse_upnp_time("00:00:01"), 1000)
        self.assertEqual(upnp.parse_upnp_time("1:02:03"), 3_723_000)
        self.assertEqual(upnp.parse_upnp_time("10:00:00"), 36_000_000)

    def test_parse_milliseconds(self) -> None:
        self.assertEqual(upnp.parse_upnp_time("00:00:01.500"), 1500)
        self.assertEqual(upnp.parse_upnp_time("0:00:00.250"), 250)
        self.assertEqual(upnp.parse_upnp_time("0:01:02.001"), 62_001)

    def test_parse_not_implemented_and_empty(self) -> None:
        self.assertIsNone(upnp.parse_upnp_time("NOT_IMPLEMENTED"))
        self.assertIsNone(upnp.parse_upnp_time("not_implemented"))
        self.assertIsNone(upnp.parse_upnp_time(""))
        self.assertIsNone(upnp.parse_upnp_time("   "))
        self.assertIsNone(upnp.parse_upnp_time(None))

    def test_parse_invalid(self) -> None:
        self.assertIsNone(upnp.parse_upnp_time("garbage"))
        self.assertIsNone(upnp.parse_upnp_time("12:34"))
        self.assertIsNone(upnp.parse_upnp_time("1:2:3:4"))
        self.assertIsNone(upnp.parse_upnp_time("0:00:xx"))
        self.assertIsNone(upnp.parse_upnp_time("-1:00:00"))

    def test_format(self) -> None:
        self.assertEqual(upnp.format_upnp_time(0), "00:00:00")
        self.assertEqual(upnp.format_upnp_time(59), "00:00:59")
        self.assertEqual(upnp.format_upnp_time(60), "00:01:00")
        self.assertEqual(upnp.format_upnp_time(3661), "01:01:01")
        # 小时位不截断。
        self.assertEqual(upnp.format_upnp_time(90_061), "25:01:01")
        self.assertEqual(upnp.format_upnp_time(-5), "00:00:00")

    def test_round_trip(self) -> None:
        for text in ("0:00:00", "00:01:05", "3:04:05"):
            ms = upnp.parse_upnp_time(text)
            self.assertIsNotNone(ms)
            assert ms is not None
            self.assertEqual(upnp.format_upnp_time(ms // 1000), f"{int(text.split(':')[0]):02d}:{text.split(':')[1]}:{text.split(':')[2]}")


class ProtocolInfoTests(unittest.TestCase):
    """parse_protocol_info / pick_stream_mime。"""

    def test_parse_protocol_info(self) -> None:
        self.assertEqual(
            upnp.parse_protocol_info("http-get:*:audio/wav:*, http-get:*:audio/L16;rate=44100;channels=2:*"),
            ["http-get:*:audio/wav:*", "http-get:*:audio/L16;rate=44100;channels=2:*"],
        )
        self.assertEqual(upnp.parse_protocol_info("a, b ,,c "), ["a", "b", "c"])
        self.assertEqual(upnp.parse_protocol_info(""), [])
        self.assertEqual(upnp.parse_protocol_info(None), [])

    def test_pick_wav(self) -> None:
        self.assertEqual(upnp.pick_stream_mime(["http-get:*:audio/wav:*"]), ("audio/wav", "wav"))
        self.assertEqual(upnp.pick_stream_mime(["http-get:*:audio/x-wav:*"]), ("audio/wav", "wav"))
        self.assertEqual(upnp.pick_stream_mime(["http-get:*:audio/wave:*"]), ("audio/wav", "wav"))

    def test_pick_prefers_wav_over_l16_regardless_of_order(self) -> None:
        entries = [
            "http-get:*:audio/L16;rate=44100;channels=2:*",
            "http-get:*:audio/wav:*",
        ]
        self.assertEqual(upnp.pick_stream_mime(entries), ("audio/wav", "wav"))

    def test_pick_l16_normalizes_rate_and_channels(self) -> None:
        self.assertEqual(
            upnp.pick_stream_mime(["http-get:*:audio/L16;rate=48000;channels=1:*"]),
            ("audio/L16;rate=44100;channels=2", "l16"),
        )
        self.assertEqual(
            upnp.pick_stream_mime(["http-get:*:audio/l16:*"]),
            ("audio/L16;rate=44100;channels=2", "l16"),
        )

    def test_pick_fallback_keeps_original_mime(self) -> None:
        self.assertEqual(
            upnp.pick_stream_mime(["http-get:*:audio/L24;rate=96000;channels=2:*"]),
            ("audio/L24;rate=96000;channels=2", "l16"),
        )
        self.assertEqual(upnp.pick_stream_mime(["http-get:*:audio/basic:*"]), ("audio/basic", "l16"))
        self.assertEqual(upnp.pick_stream_mime(["http-get:*:audio/L8:*"]), ("audio/L8", "l16"))

    def test_pick_ignores_non_http_get_and_malformed(self) -> None:
        self.assertIsNone(upnp.pick_stream_mime(["rtsp:*:audio/wav:*"]))
        self.assertIsNone(upnp.pick_stream_mime(["garbage"]))
        self.assertIsNone(upnp.pick_stream_mime(["http-get:*"]))
        self.assertIsNone(upnp.pick_stream_mime([]))
        self.assertIsNone(upnp.pick_stream_mime(None))

    def test_pick_skips_empty_entries(self) -> None:
        entries = ["", "   ", "http-get:*:audio/wav:*"]
        self.assertEqual(upnp.pick_stream_mime(entries), ("audio/wav", "wav"))


class SoapEnvelopeTests(unittest.TestCase):
    """build_soap_envelope 的结构与转义。"""

    def test_envelope_structure(self) -> None:
        xml = upnp.build_soap_envelope(
            upnp.AVTRANSPORT, "GetTransportInfo", {"InstanceID": "0"}
        )
        self.assertTrue(xml.startswith('<?xml version="1.0" encoding="utf-8"?>'))
        self.assertIn('xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"', xml)
        self.assertIn(
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"', xml
        )
        self.assertIn(f'xmlns:u="{upnp.AVTRANSPORT}"', xml)
        self.assertIn("<u:GetTransportInfo", xml)
        self.assertIn("<InstanceID>0</InstanceID>", xml)
        self.assertIn("</u:GetTransportInfo>", xml)
        self.assertIn("<s:Body>", xml)
        self.assertIn("</s:Envelope>", xml)

    def test_envelope_is_well_formed_xml(self) -> None:
        xml = upnp.build_soap_envelope(
            upnp.AVTRANSPORT,
            "SetAVTransportURI",
            {"InstanceID": "0", "CurrentURI": "http://h/a?x=1&y=2", "CurrentURIMetaData": "<DIDL/>"},
        )
        root = ET.fromstring(xml)
        action = root.find(f".//{{{SOAP_ENV_NS}}}Body")
        self.assertIsNotNone(action)
        # 参数值必须被转义。
        self.assertIn("&amp;", xml)
        self.assertIn("&lt;DIDL/&gt;", xml)
        current_uri = None
        for element in root.iter():
            if element.tag.endswith("CurrentURI"):
                current_uri = element.text
        self.assertEqual(current_uri, "http://h/a?x=1&y=2")

    def test_empty_args(self) -> None:
        xml = upnp.build_soap_envelope(upnp.CONNECTIONMANAGER, "GetProtocolInfo", {})
        self.assertIn("<u:GetProtocolInfo", xml)
        self.assertIn("</u:GetProtocolInfo>", xml)


class ParseSoapResponseTests(unittest.TestCase):
    """parse_soap_response 的成功与故障分支。"""

    def test_success_arguments(self) -> None:
        out = upnp.parse_soap_response(SUCCESS_XML)
        self.assertEqual(out["CurrentTransportState"], "PLAYING")
        self.assertEqual(out["CurrentTransportStatus"], "OK")
        self.assertEqual(out["CurrentSpeed"], "1")

    def test_success_accepts_str_and_strips(self) -> None:
        xml = (
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            '<u:GetVolumeResponse xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
            "<CurrentVolume> 42 </CurrentVolume>"
            "</u:GetVolumeResponse></s:Body></s:Envelope>"
        )
        out = upnp.parse_soap_response(xml)
        self.assertEqual(out["CurrentVolume"], "42")

    def test_no_response_element_returns_empty_dict(self) -> None:
        xml = (
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body/>'
            "</s:Envelope>"
        )
        self.assertEqual(upnp.parse_soap_response(xml), {})

    def test_fault_raises_with_code_and_description(self) -> None:
        with self.assertRaises(upnp.UpnpError) as ctx:
            upnp.parse_soap_response(FAULT_XML)
        error = ctx.exception
        self.assertEqual(error.code, 402)
        self.assertEqual(error.description, "Invalid Args")
        self.assertIn("402", str(error))

    def test_fault_without_upnp_error_detail(self) -> None:
        with self.assertRaises(upnp.UpnpError) as ctx:
            upnp.parse_soap_response(FAULT_STRING_ONLY_XML)
        error = ctx.exception
        self.assertIsNone(error.code)
        self.assertEqual(error.description, "Something went wrong")

    def test_invalid_and_empty(self) -> None:
        with self.assertRaises(upnp.UpnpError):
            upnp.parse_soap_response(b"<not-xml")
        with self.assertRaises(upnp.UpnpError):
            upnp.parse_soap_response(b"")


class DeviceDescriptionTests(unittest.TestCase):
    """parse_device_description 的命名空间/相对 URL/嵌套设备处理。"""

    XML = """<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>Living Room Renderer</friendlyName>
    <manufacturer>Acme Corp</manufacturer>
    <modelName>AcmeRenderer 3000</modelName>
    <UDN>uuid:11111111-2222-3333-4444-555555555555</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:2</serviceType>
        <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
        <controlURL>/AVTransport/control</controlURL>
        <eventSubURL>/AVTransport/event</eventSubURL>
        <SCPDURL>/AVTransport/scpd.xml</SCPDURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>
        <controlURL>../RenderingControl/control</controlURL>
        <eventSubURL>../RenderingControl/event</eventSubURL>
        <SCPDURL>../RenderingControl/scpd.xml</SCPDURL>
      </service>
      <service>
        <serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>
        <controlURL>ContentDir/control</controlURL>
        <eventSubURL></eventSubURL>
        <SCPDURL>ContentDir/scpd.xml</SCPDURL>
      </service>
    </serviceList>
    <deviceList>
      <device>
        <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
        <friendlyName>Embedded Renderer</friendlyName>
        <UDN>uuid:embedded</UDN>
        <serviceList>
          <service>
            <serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>
            <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
            <controlURL>/ConnectionManager/control</controlURL>
            <eventSubURL>/ConnectionManager/event</eventSubURL>
            <SCPDURL>/ConnectionManager/scpd.xml</SCPDURL>
          </service>
        </serviceList>
      </device>
    </deviceList>
  </device>
</root>
"""

    LOCATION = "http://192.168.1.50:49152/desc.xml"

    def test_parses_first_mediarenderer_and_services(self) -> None:
        info = upnp.parse_device_description(self.XML, self.LOCATION, "192.168.1.50")
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.friendly_name, "Living Room Renderer")
        self.assertEqual(info.udn, "uuid:11111111-2222-3333-4444-555555555555")
        self.assertEqual(info.manufacturer, "Acme Corp")
        self.assertEqual(info.model_name, "AcmeRenderer 3000")
        self.assertIn("MediaRenderer", info.device_type)
        self.assertEqual(info.ip, "192.168.1.50")
        self.assertEqual(info.location, self.LOCATION)

        # :2 的 AVTransport 映射到标准 :1 键。
        avt = info.services[upnp.AVTRANSPORT]
        self.assertEqual(avt.service_type, "urn:schemas-upnp-org:service:AVTransport:2")
        self.assertEqual(avt.control_url, "http://192.168.1.50:49152/AVTransport/control")
        self.assertEqual(avt.event_sub_url, "http://192.168.1.50:49152/AVTransport/event")
        self.assertEqual(avt.scpd_url, "http://192.168.1.50:49152/AVTransport/scpd.xml")

        rc = info.services[upnp.RENDERINGCONTROL]
        self.assertEqual(rc.control_url, "http://192.168.1.50:49152/RenderingControl/control")
        self.assertEqual(rc.event_sub_url, "http://192.168.1.50:49152/RenderingControl/event")

        # 内嵌设备中的服务也要被收集。
        cm = info.services[upnp.CONNECTIONMANAGER]
        self.assertEqual(cm.control_url, "http://192.168.1.50:49152/ConnectionManager/control")

        # 未知服务类型保留原始 serviceType 作为键。
        self.assertIn("urn:schemas-upnp-org:service:ContentDirectory:1", info.services)

        self.assertTrue(info.has(upnp.AVTRANSPORT))
        self.assertTrue(info.has(upnp.RENDERINGCONTROL))
        self.assertFalse(info.has("urn:schemas-upnp-org:service:Unknown:1"))

    def test_relative_url_resolved_against_location_path(self) -> None:
        xml = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <friendlyName>R</friendlyName>
    <UDN>uuid:rel</UDN>
    <serviceList>
      <service>
        <serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>
        <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
        <controlURL>AVTransport/control</controlURL>
        <eventSubURL>AVTransport/event</eventSubURL>
        <SCPDURL>AVTransport/scpd.xml</SCPDURL>
      </service>
    </serviceList>
  </device>
</root>
"""
        info = upnp.parse_device_description(xml, "http://host:1234/upnp/desc.xml", "10.0.0.1")
        assert info is not None
        self.assertEqual(
            info.services[upnp.AVTRANSPORT].control_url,
            "http://host:1234/upnp/AVTransport/control",
        )

    def test_missing_optional_fields_default_to_empty(self) -> None:
        xml = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
    <UDN>uuid:min</UDN>
  </device>
</root>
"""
        info = upnp.parse_device_description(xml, self.LOCATION, "10.0.0.2")
        assert info is not None
        self.assertEqual(info.friendly_name, "")
        self.assertEqual(info.manufacturer, "")
        self.assertEqual(info.model_name, "")
        self.assertEqual(info.services, {})

    def test_returns_none_without_mediarenderer(self) -> None:
        xml = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <UDN>uuid:server</UDN>
  </device>
</root>
"""
        self.assertIsNone(upnp.parse_device_description(xml, self.LOCATION, "10.0.0.3"))

    def test_returns_none_on_invalid_or_empty_xml(self) -> None:
        self.assertIsNone(upnp.parse_device_description("<not-xml", self.LOCATION, "10.0.0.4"))
        self.assertIsNone(upnp.parse_device_description("", self.LOCATION, "10.0.0.4"))
        self.assertIsNone(upnp.parse_device_description("   ", self.LOCATION, "10.0.0.4"))


class UpnpClientHttpTests(unittest.TestCase):
    """UpnpClient 针对本地 HTTP 服务器的 SOAP/GENA 行为。"""

    def setUp(self) -> None:
        self.server = _TestServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02})
        self.thread.daemon = True
        self.thread.start()
        self.addCleanup(self._stop_server)

        base = f"http://127.0.0.1:{self.port}"
        self.device = upnp.DeviceInfo(
            udn="uuid:test-device",
            friendly_name="Test",
            manufacturer="",
            model_name="",
            device_type="urn:schemas-upnp-org:device:MediaRenderer:1",
            ip="127.0.0.1",
            location=f"{base}/desc.xml",
            services={
                upnp.AVTRANSPORT: upnp.Service(
                    service_type=upnp.AVTRANSPORT,
                    service_id="urn:upnp-org:serviceId:AVTransport",
                    control_url=f"{base}/AVTransport/control",
                    event_sub_url=f"{base}/AVTransport/event",
                    scpd_url=f"{base}/AVTransport/scpd.xml",
                ),
                upnp.RENDERINGCONTROL: upnp.Service(
                    service_type=upnp.RENDERINGCONTROL,
                    service_id="urn:upnp-org:serviceId:RenderingControl",
                    control_url=f"{base}/RenderingControl/control",
                    event_sub_url=f"{base}/RenderingControl/event",
                    scpd_url=f"{base}/RenderingControl/scpd.xml",
                ),
            },
        )

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    @staticmethod
    def _lower_headers(headers: dict[str, str]) -> dict[str, str]:
        return {str(key).lower(): value for key, value in headers.items()}

    def test_soap_request_headers_and_body(self) -> None:
        self.server.responder = lambda _body: (200, SUCCESS_XML)
        client = upnp.UpnpClient(self.device, timeout=2.0)

        result = client.soap(upnp.AVTRANSPORT, "GetTransportInfo", {"InstanceID": "0"})

        self.assertEqual(result["CurrentTransportState"], "PLAYING")
        self.assertEqual(result["CurrentTransportStatus"], "OK")
        self.assertEqual(result["CurrentSpeed"], "1")

        self.assertEqual(len(self.server.requests), 1)
        recorded = self.server.requests[0]
        self.assertEqual(recorded["path"], "/AVTransport/control")
        headers = self._lower_headers(recorded["headers"])
        self.assertEqual(headers["soapaction"], f'"{upnp.AVTRANSPORT}#GetTransportInfo"')
        self.assertEqual(headers["content-type"], 'text/xml; charset="utf-8"')
        self.assertEqual(headers["connection"], "close")

        body = recorded["body"].decode("utf-8")
        self.assertIn('<?xml version="1.0" encoding="utf-8"?>', body)
        self.assertIn(f'xmlns:u="{upnp.AVTRANSPORT}"', body)
        self.assertIn("<u:GetTransportInfo", body)
        self.assertIn("<InstanceID>0</InstanceID>", body)
        root = ET.fromstring(body)
        self.assertIsNotNone(root.find(f".//{{{SOAP_ENV_NS}}}Body"))

    def test_soap_fault_becomes_upnp_error(self) -> None:
        self.server.responder = lambda _body: (500, FAULT_XML)
        client = upnp.UpnpClient(self.device, timeout=2.0)

        with self.assertRaises(upnp.UpnpError) as ctx:
            client.soap(upnp.AVTRANSPORT, "GetTransportInfo", {"InstanceID": "0"})
        self.assertEqual(ctx.exception.code, 402)
        self.assertEqual(ctx.exception.description, "Invalid Args")

    def test_soap_http_error_without_fault(self) -> None:
        self.server.responder = lambda _body: (500, b"<html>server exploded</html>")
        client = upnp.UpnpClient(self.device, timeout=2.0)

        with self.assertRaises(upnp.UpnpError) as ctx:
            client.soap(upnp.AVTRANSPORT, "GetTransportInfo", {"InstanceID": "0"})
        self.assertIsNone(ctx.exception.code)
        self.assertIn("500", str(ctx.exception))

    def test_soap_unknown_service_raises(self) -> None:
        client = upnp.UpnpClient(self.device, timeout=2.0)
        with self.assertRaises(upnp.UpnpError):
            client.soap(upnp.CONNECTIONMANAGER, "GetProtocolInfo", {})

    def test_soap_connection_error_raises(self) -> None:
        # 绑定后立即关闭，得到一个几乎肯定拒绝连接的端口。
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
        probe.close()

        base = f"http://127.0.0.1:{closed_port}"
        device = upnp.DeviceInfo(
            udn="uuid:dead",
            friendly_name="Dead",
            manufacturer="",
            model_name="",
            device_type="",
            ip="127.0.0.1",
            location=f"{base}/desc.xml",
            services={
                upnp.AVTRANSPORT: upnp.Service(
                    service_type=upnp.AVTRANSPORT,
                    service_id="sid",
                    control_url=f"{base}/AVTransport/control",
                    event_sub_url=f"{base}/AVTransport/event",
                    scpd_url=f"{base}/AVTransport/scpd.xml",
                )
            },
        )
        client = upnp.UpnpClient(device, timeout=1.0)
        with self.assertRaises(upnp.UpnpError):
            client.soap(upnp.AVTRANSPORT, "Play", {"InstanceID": "0", "Speed": "1"})

    def test_soap_convenience_wrappers_build_expected_args(self) -> None:
        self.server.responder = lambda _body: (
            200,
            b'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
            b'<u:GetVolumeResponse xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
            b"<CurrentVolume>37</CurrentVolume></u:GetVolumeResponse></s:Body></s:Envelope>",
        )
        client = upnp.UpnpClient(self.device, timeout=2.0)
        self.assertEqual(client.get_volume(), 37)
        body = self.server.requests[-1]["body"].decode("utf-8")
        self.assertIn("<Channel>Master</Channel>", body)
        self.assertIn("<InstanceID>0</InstanceID>", body)

    def test_thread_safety(self) -> None:
        self.server.responder = lambda _body: (200, SUCCESS_XML)
        client = upnp.UpnpClient(self.device, timeout=2.0)
        results: list[dict] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                info = client.get_transport_info()
                with lock:
                    results.append(info)
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10.0)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertTrue(all(item["state"] == "PLAYING" for item in results))
        self.assertEqual(len(self.server.requests), 8)

    def test_subscribe_renew_unsubscribe(self) -> None:
        self.server.sid = "uuid:sub-1234"
        client = upnp.UpnpClient(self.device, timeout=2.0)

        sid = client.subscribe("http://127.0.0.1:9999/notify", timeout_s=1800)
        self.assertEqual(sid, "uuid:sub-1234")
        headers = self._lower_headers(self.server.requests[-1]["headers"])
        self.assertEqual(headers["callback"], "<http://127.0.0.1:9999/notify>")
        self.assertEqual(headers["nt"], "upnp:event")
        self.assertEqual(headers["timeout"], "Second-1800")
        self.assertEqual(self.server.requests[-1]["method"], "SUBSCRIBE")

        self.assertTrue(client.renew_subscription("uuid:sub-1234", timeout_s=300))
        headers = self._lower_headers(self.server.requests[-1]["headers"])
        self.assertEqual(headers["sid"], "uuid:sub-1234")
        self.assertEqual(headers["timeout"], "Second-300")

        self.assertTrue(client.unsubscribe("uuid:sub-1234"))
        self.assertEqual(self.server.requests[-1]["method"], "UNSUBSCRIBE")
        headers = self._lower_headers(self.server.requests[-1]["headers"])
        self.assertEqual(headers["sid"], "uuid:sub-1234")

    def test_subscribe_without_sid_returns_none(self) -> None:
        self.server.sid = ""
        client = upnp.UpnpClient(self.device, timeout=2.0)
        self.assertIsNone(client.subscribe("http://127.0.0.1:9999/notify"))


class FetchDeviceTests(unittest.TestCase):
    """fetch_device 针对本地 HTTP 服务器的抓取与解析。"""

    def setUp(self) -> None:
        self.server = _TestServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02})
        self.thread.daemon = True
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)

    def test_fetch_device_success(self) -> None:
        self.server.get_body = DEVICE_XML
        location = f"http://127.0.0.1:{self.port}/desc.xml"
        info = upnp.fetch_device(location, "127.0.0.1", timeout=2.0, retries=0)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.friendly_name, "Network Speaker")
        self.assertEqual(info.udn, "uuid:dddddddd-0000-0000-0000-000000000000")
        self.assertEqual(info.manufacturer, "Acme")
        self.assertEqual(info.model_name, "Speaker 100")
        self.assertEqual(info.ip, "127.0.0.1")
        self.assertEqual(
            info.services[upnp.AVTRANSPORT].control_url,
            f"http://127.0.0.1:{self.port}/AVTransport/control",
        )

    def test_fetch_device_returns_none_on_http_error(self) -> None:
        self.server.get_status = 404
        self.server.get_body = b"not found"
        info = upnp.fetch_device(
            f"http://127.0.0.1:{self.port}/desc.xml", "127.0.0.1", timeout=2.0, retries=0
        )
        self.assertIsNone(info)

    def test_fetch_device_returns_none_on_connection_error(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
        probe.close()
        self.assertIsNone(
            upnp.fetch_device(f"http://127.0.0.1:{closed_port}/desc.xml", "127.0.0.1", timeout=1.0, retries=0)
        )

    def test_fetch_device_returns_none_for_bad_scheme(self) -> None:
        self.assertIsNone(upnp.fetch_device("", "127.0.0.1", timeout=1.0, retries=0))
        self.assertIsNone(upnp.fetch_device("file:///etc/hostname", "127.0.0.1", timeout=1.0, retries=0))

    def test_fetch_device_returns_none_without_mediarenderer(self) -> None:
        self.server.get_body = b'<?xml version="1.0"?><root><device><deviceType>urn:x:MediaServer:1</deviceType></device></root>'
        self.assertIsNone(
            upnp.fetch_device(f"http://127.0.0.1:{self.port}/desc.xml", "127.0.0.1", timeout=2.0, retries=0)
        )

    def test_fetch_device_retries_then_gives_up(self) -> None:
        self.server.get_status = 500
        self.server.get_body = b"boom"
        info = upnp.fetch_device(
            f"http://127.0.0.1:{self.port}/desc.xml", "127.0.0.1", timeout=2.0, retries=2
        )
        self.assertIsNone(info)
        # retries=2 -> 共 3 次尝试。
        get_requests = [r for r in self.server.requests if r["method"] == "GET"]
        self.assertEqual(len(get_requests), 3)


if __name__ == "__main__":
    unittest.main()
