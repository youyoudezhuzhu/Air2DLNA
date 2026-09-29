#!/usr/bin/env python3
"""模拟一个 UPnP AV MediaRenderer，用于端到端验证 AirPlay 2 → DLNA 桥接。

它实现了真实渲染器的三件事：

1. **SSDP 响应**：监听 239.255.255.250:1900，收到 M-SEARCH 后按
   `urn:schemas-upnp-org:device:MediaRenderer:1` 回复 LOCATION。
2. **设备描述 + SOAP**：提供 device description XML（故意使用**相对** controlURL
   以验证 URL 解析），并处理 AVTransport / RenderingControl / ConnectionManager 动作。
3. **真的去拉流**：收到 `SetAVTransportURI` 后，用后台线程 GET 那个 URI，
   记录收到的字节（用于断言 WAV 头与 PCM 内容）。

所有动作都会记录到内存，可通过 `GET /__record` 取出 JSON，便于测试断言。

用法::

    python3 fake_renderer.py --http-port 0 --ssdp-port 1900 --record /tmp/rec.json
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import struct
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MEDIA_RENDERER = "urn:schemas-upnp-org:device:MediaRenderer:1"
AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RCS = "urn:schemas-upnp-org:service:RenderingControl:1"
CMS = "urn:schemas-upnp-org:service:ConnectionManager:1"

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900


class State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.events: list[dict] = []
        self.transport_state = "STOPPED"
        self.current_uri = ""
        self.current_uri_metadata = ""
        self.volume = 50
        self.mute = False
        self.position_ms = 0
        self.duration_ms = 0
        self.stream_bytes = bytearray()
        self.stream_content_type = ""
        self.stream_status: int | None = None
        self.stream_open_count = 0
        self.stream_error = ""

    def record(self, action: str, args: dict | None = None, extra: dict | None = None) -> None:
        with self.lock:
            entry = {"t": round(time.monotonic(), 3), "action": action, "args": args or {}}
            if extra:
                entry.update(extra)
            self.events.append(entry)

    def to_dict(self) -> dict:
        with self.lock:
            return {
                "events": list(self.events),
                "transport_state": self.transport_state,
                "current_uri": self.current_uri,
                "current_uri_metadata": self.current_uri_metadata,
                "volume": self.volume,
                "mute": self.mute,
                "stream_bytes_len": len(self.stream_bytes),
                "stream_first_64_hex": bytes(self.stream_bytes[:64]).hex(),
                "stream_content_type": self.stream_content_type,
                "stream_status": self.stream_status,
                "stream_open_count": self.stream_open_count,
                "stream_error": self.stream_error,
            }


STATE = State()


def device_description(host: str, port: int) -> bytes:
    """注意：controlURL/eventSubURL 故意写成相对路径，用于验证 urljoin 解析。"""
    return f'''<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>{MEDIA_RENDERER}</deviceType>
    <friendlyName>Fake Living Room Speaker</friendlyName>
    <manufacturer>TestCo</manufacturer>
    <modelName>FakeRenderer-1</modelName>
    <UDN>uuid:fake-renderer-0001</UDN>
    <serviceList>
      <service>
        <serviceType>{AVT}</serviceType>
        <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
        <SCPDURL>/AVTransport/scpd.xml</SCPDURL>
        <controlURL>AVTransport/control</controlURL>
        <eventSubURL>AVTransport/event</eventSubURL>
      </service>
      <service>
        <serviceType>{RCS}</serviceType>
        <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>
        <SCPDURL>/RenderingControl/scpd.xml</SCPDURL>
        <controlURL>/RenderingControl/control</controlURL>
        <eventSubURL>/RenderingControl/event</eventSubURL>
      </service>
      <service>
        <serviceType>{CMS}</serviceType>
        <serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>
        <SCPDURL>/ConnectionManager/scpd.xml</SCPDURL>
        <controlURL>/ConnectionManager/control</controlURL>
        <eventSubURL>/ConnectionManager/event</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>
'''.encode("utf-8")


def _env(action: str, service: str, out: dict[str, str] | None = None) -> bytes:
    args = "".join(f"<{k}>{v}</{k}>" for k, v in (out or {}).items())
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action}Response xmlns:u="{service}">{args}</u:{action}Response>'
        "</s:Body></s:Envelope>"
    ).encode("utf-8")


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _parse_action(body: bytes) -> tuple[str, str, dict[str, str]]:
    root = ET.fromstring(body)
    body_el = next(e for e in root.iter() if _local(e.tag) == "Body")
    action_el = list(body_el)[0]
    action = _local(action_el.tag)
    service = ""
    for key, value in action_el.attrib.items():
        if _local(key) == "u" or key.endswith("}u") or key == "u":
            service = value
    if not service:
        # 从 xmlns:u 抓取
        for key, value in action_el.attrib.items():
            if key.endswith("u"):
                service = value
    args = {_local(child.tag): (child.text or "") for child in action_el}
    return action, service, args


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeRenderer/1.0"

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _xml(self, body: bytes, status: int = 200, action: str = "", service: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(body)))
        if action:
            self.send_header("EXT", "")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        host, port = self.server.server_address[0], self.server.server_address[1]
        if path in ("/desc.xml", "/"):
            self._xml(device_description(host, port))
            return
        if path.endswith("/scpd.xml"):
            self._xml(b"<scpd/>")
            return
        if path == "/__record":
            self._json(STATE.to_dict())
            return
        self._json({"error": "not found", "path": path}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        soap_action = (self.headers.get("SOAPACTION") or "").strip('"')
        try:
            action, service, args = _parse_action(body)
        except Exception as exc:  # noqa: BLE001
            STATE.record("PARSE_ERROR", {"detail": str(exc), "raw": soap_action})
            self._json({"error": "bad soap"}, status=400)
            return

        # 校验 SOAPACTION 头与 body 中的动作一致（真实的互操作检查）
        header_action = soap_action.split("#")[-1] if "#" in soap_action else soap_action
        STATE.record(
            action,
            args,
            {"soapaction": soap_action, "header_matches_body": header_action == action},
        )
        self._dispatch(action, service, args)

    def _dispatch(self, action: str, service: str, args: dict[str, str]) -> None:
        with STATE.lock:
            if action == "GetTransportInfo":
                out = {
                    "CurrentTransportState": STATE.transport_state,
                    "CurrentTransportStatus": "OK",
                    "CurrentSpeed": "1",
                }
            elif action == "GetPositionInfo":
                out = {
                    "Track": "1",
                    "TrackDuration": _hms(STATE.duration_ms),
                    "TrackMetaData": STATE.current_uri_metadata,
                    "TrackURI": STATE.current_uri,
                    "RelTime": _hms(STATE.position_ms),
                    "AbsTime": _hms(STATE.position_ms),
                }
            elif action == "GetMediaInfo":
                out = {
                    "NrTracks": "1",
                    "MediaDuration": _hms(STATE.duration_ms),
                    "CurrentURI": STATE.current_uri,
                    "CurrentURIMetaData": STATE.current_uri_metadata,
                }
            elif action == "GetVolume":
                out = {"CurrentVolume": str(STATE.volume)}
            elif action == "GetMute":
                out = {"CurrentMute": "1" if STATE.mute else "0"}
            elif action == "GetProtocolInfo":
                out = {
                    "Source": "",
                    "Sink": (
                        "http-get:*:audio/mpeg:*,"
                        "http-get:*:audio/wav:*,"
                        "http-get:*:audio/L16;rate=44100;channels=2:*"
                    ),
                }
            elif action == "SetAVTransportURI":
                STATE.current_uri = args.get("CurrentURI", "")
                STATE.current_uri_metadata = args.get("CurrentURIMetaData", "")
                STATE.stream_bytes = bytearray()
                STATE.stream_error = ""
                STATE.transport_state = "STOPPED"
                threading.Thread(
                    target=_fetch_stream, args=(STATE.current_uri,), daemon=True
                ).start()
                out = {}
            elif action == "SetVolume":
                try:
                    STATE.volume = int(float(args.get("DesiredVolume", "0")))
                except ValueError:
                    pass
                out = {}
            elif action == "SetMute":
                STATE.mute = args.get("DesiredMute", "0") in ("1", "true", "True")
                out = {}
            elif action == "Play":
                STATE.transport_state = "PLAYING"
                out = {}
            elif action == "Pause":
                STATE.transport_state = "PAUSED_PLAYBACK"
                out = {}
            elif action == "Stop":
                STATE.transport_state = "STOPPED"
                out = {}
            elif action == "Seek":
                out = {}
            elif action in ("Subscribe", "Renew"):
                out = {}
            else:
                out = {}
        self._xml(_env(action, service or AVT, out), action=action, service=service)


def _fetch_stream(uri: str) -> None:
    """真的把桥接器提供的实时流拉下来（模拟渲染器行为）。"""
    if not uri:
        return
    with STATE.lock:
        STATE.stream_open_count += 1
    try:
        request = urllib.request.Request(uri, headers={"getcontentFeatures.dlna.org": "1"})
        with urllib.request.urlopen(request, timeout=15) as response:
            with STATE.lock:
                STATE.stream_status = response.status
                STATE.stream_content_type = response.headers.get("Content-Type", "")
                transfer_mode = response.headers.get("transferMode.dlna.org", "")
                features = response.headers.get("contentFeatures.dlna.org", "")
            STATE.record(
                "STREAM_HEADERS",
                {
                    "status": response.status,
                    "content_type": response.headers.get("Content-Type", ""),
                    "transfer_mode": transfer_mode,
                    "features": features[:60],
                    "content_length": response.headers.get("Content-Length", ""),
                },
            )
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                chunk = response.read(8192)
                if not chunk:
                    break
                with STATE.lock:
                    STATE.stream_bytes.extend(chunk)
                    # 模拟渲染器按 1x 消费：读满约 1 秒音频就推进位置
                    bytes_per_sec = 44100 * 4
                    STATE.position_ms = int(
                        len(STATE.stream_bytes) / bytes_per_sec * 1000
                    )
    except Exception as exc:  # noqa: BLE001
        with STATE.lock:
            STATE.stream_error = f"{type(exc).__name__}: {exc}"
        STATE.record("STREAM_ERROR", {"detail": str(exc)})


def _hms(ms: int) -> str:
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{millis:03d}"


class SsdpResponder(threading.Thread):
    def __init__(self, location: str, port: int = SSDP_PORT) -> None:
        super().__init__(name="ssdp-responder", daemon=True)
        self.location = location
        self.port = port
        self.stop_event = threading.Event()
        self.replies = 0
        self.error = ""

    def run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(("", self.port))
            membership = struct.pack("4s4s", socket.inet_aton(SSDP_ADDR),
                                     socket.inet_aton("0.0.0.0"))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            sock.settimeout(0.5)
        except OSError as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return

        while not self.stop_event.is_set():
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            text = data.decode("latin-1", "replace")
            if not text.upper().startswith("M-SEARCH"):
                continue
            st = ""
            for line in text.splitlines():
                if line.lower().startswith("st:"):
                    st = line.split(":", 1)[1].strip()
            response = (
                "HTTP/1.1 200 OK\r\n"
                "CACHE-CONTROL: max-age=1800\r\n"
                "EXT:\r\n"
                f"LOCATION: {self.location}\r\n"
                "SERVER: TestOS/1.0 UPnP/1.0 FakeRenderer/1.0\r\n"
                f"ST: {st or MEDIA_RENDERER}\r\n"
                "USN: uuid:fake-renderer-0001::" + (st or MEDIA_RENDERER) + "\r\n"
                "\r\n"
            ).encode("latin-1")
            try:
                sock.sendto(response, addr)
                self.replies += 1
            except OSError:
                pass
        sock.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http-port", type=int, default=0)
    parser.add_argument("--ssdp-port", type=int, default=SSDP_PORT)
    parser.add_argument("--announce-ip", default="")
    parser.add_argument("--record", default="")
    args = parser.parse_args()

    httpd = ThreadingHTTPServer(("0.0.0.0", args.http_port), Handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    ip = args.announce_ip or _primary_ip()
    location = f"http://{ip}:{port}/desc.xml"

    responder = SsdpResponder(location, args.ssdp_port)
    responder.start()

    print(json.dumps({
        "http_port": port,
        "ssdp_port": args.ssdp_port,
        "location": location,
        "udn": "uuid:fake-renderer-0001",
        "ssdp_error": responder.error,
    }), flush=True)

    def _shutdown(_signum, _frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        httpd.serve_forever()
    finally:
        responder.stop_event.set()
        if args.record:
            try:
                with open(args.record, "w", encoding="utf-8") as handle:
                    json.dump(STATE.to_dict(), handle, ensure_ascii=False, indent=2)
            except OSError:
                pass
    return 0


def _primary_ip() -> str:
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("223.5.5.5", 53))
            return probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return "127.0.0.1"


if __name__ == "__main__":
    sys.exit(main())
