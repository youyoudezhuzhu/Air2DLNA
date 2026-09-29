#!/usr/bin/env python3
"""端到端集成测试：AirPlay 2 事件 → 桥接器 → DLNA 渲染器 → 实时 PCM 流。

本测试**不使用**任何 mock 掉被测逻辑的替身：

* AirPlay 侧：按照 shairport-sync 5.5.1 ``metadata/pipe.c`` 的**真实线格式**
  （hex 类型/代码 + base64 载荷）向元数据 FIFO 写事件，并按真实音频管道格式
  写 S16LE/44100/2ch PCM。
* DLNA 侧：``tests/fake_renderer.py`` 提供**真实**的 SSDP 响应、设备描述、SOAP
  控制端点，并真的通过 HTTP 拉取桥接器提供的音频流。

因此它验证的是真正的数据通路：FIFO → 解析 → 状态机 → SOAP → HTTP 流 → 渲染器。

用法::

    python3 tests/integration_test.py            # 需要本机 1900/UDP 可用

退出码 0 表示全部通过。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "app", "server")
BRIDGE = os.path.join(SERVER, "bridge.py")
FAKE = os.path.join(ROOT, "tests", "fake_renderer.py")

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), name, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    return bool(ok)


# --------------------------------------------------------------- 元数据线格式
def hexcode(code: str) -> str:
    """4 字符代码 -> shairport-sync 实际发出的 8 位十六进制（%x 的结果）。"""
    return code.encode("ascii").hex()


def item(type_code: str, code: str, payload: bytes | None = None) -> bytes:
    head = (
        f"<item><type>{hexcode(type_code)}</type><code>{hexcode(code)}</code>"
        f"<length>{len(payload) if payload else 0}</length>"
    ).encode("ascii")
    if not payload:
        return head + b"</item>\n"
    encoded = base64.b64encode(payload)
    body = b'\n<data encoding="base64">\n' + encoded + b"\n</data></item>\n"
    return head + body


class MetadataWriter:
    def __init__(self, path: str) -> None:
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def emit(self, type_code: str, code: str, payload: bytes | None = None) -> None:
        os.write(self.fd, item(type_code, code, payload))

    def ssnc(self, code: str, text: str = "") -> None:
        self.emit("ssnc", code, text.encode("utf-8") if text else None)

    def close(self) -> None:
        try:
            os.close(self.fd)
        except OSError:
            pass


class PcmFeeder(threading.Thread):
    """持续向音频 FIFO 写 S16LE/44100/2ch PCM，模拟 shairport-sync 的 pipe 后端。"""

    PATTERN = bytes(range(256)) * 4  # 1024 字节确定性图案，便于断言内容一致

    def __init__(self, path: str) -> None:
        super().__init__(name="pcm-feeder", daemon=True)
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.stop_event = threading.Event()
        self.written = 0

    def run(self) -> None:
        # 每 20ms 写 ~10ms 的音频（略快于实时，保证预滚动能达成）
        chunk = self.PATTERN * 2  # 2048 字节
        while not self.stop_event.is_set():
            try:
                self.fd.write if False else os.write(self.fd, chunk)
                self.written += len(chunk)
            except BlockingIOError:
                pass
            except OSError:
                break
            self.stop_event.wait(0.01)

    def stop(self) -> None:
        self.stop_event.set()
        try:
            os.close(self.fd)
        except OSError:
            pass


# ---------------------------------------------------------------- HTTP 工具
def http_json(url: str, method: str = "GET", body: dict | None = None, timeout: float = 8.0):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("X-Requested-With", "XMLHttpRequest")
    if data:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for(predicate, timeout: float, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
        except Exception as exc:  # noqa: BLE001
            last = None
            _ = exc
        if last:
            return last
        time.sleep(interval)
    return last


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def actions(record: dict) -> list[dict]:
    return [e for e in record.get("events", []) if e.get("action") not in ("STREAM_HEADERS",)]


def first(record: dict, action: str) -> dict | None:
    for event in record.get("events", []):
        if event.get("action") == action:
            return event
    return None


def all_of(record: dict, action: str) -> list[dict]:
    return [e for e in record.get("events", []) if e.get("action") == action]


# -------------------------------------------------------------------- 主流程
def main() -> int:
    workdir = tempfile.mkdtemp(prefix="a2d-it-")
    config_dir = os.path.join(workdir, "etc")
    var_dir = os.path.join(workdir, "var")
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(var_dir, exist_ok=True)

    http_port = free_port()
    ssdp_port = 1900
    ui_dir = os.path.join(ROOT, "app", "ui")
    bin_dir = os.path.join(SERVER, "bin")

    with open(os.path.join(config_dir, "config.json"), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "airplay_name": "Integration Test Speaker",
                "http_port": http_port,
                "rtsp_port": 17000,
                "log_level": "debug",
                "preroll_seconds": 0.5,
                "metadata_poll_seconds": 1.0,
                "drift_threshold_ms": 30000,
                "buffer_seconds": 60,
            },
            handle,
        )

    fake_proc = subprocess.Popen(
        [sys.executable, FAKE, "--http-port", "0", "--ssdp-port", str(ssdp_port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    fake_info = json.loads(fake_proc.stdout.readline() or "{}")
    print(f"[setup] fake renderer: {fake_info}")

    bridge_log = open(os.path.join(workdir, "bridge-stdout.log"), "w", encoding="utf-8")
    bridge_proc = subprocess.Popen(
        [
            sys.executable, BRIDGE,
            "--config-dir", config_dir,
            "--var-dir", var_dir,
            "--ui-dir", ui_dir,
            "--bin-dir", bin_dir,
            "--version", "integration",
            "--no-shairport",
        ],
        stdout=bridge_log, stderr=subprocess.STDOUT, cwd=SERVER,
    )

    meta = None
    feeder = None
    try:
        base = f"http://127.0.0.1:{http_port}"

        if not wait_for(lambda: http_json(f"{base}/api/health", timeout=2), 25):
            print("!! 桥接器未在 25s 内就绪，日志尾部：")
            print(_tail(os.path.join(var_dir, "bridge.log")))
            return 1
        check(True, "桥接器 HTTP 服务就绪", base)

        # ---------------------------------------------------------- 1. SSDP 发现
        found = wait_for(
            lambda: [r for r in http_json(f"{base}/api/renderers")["renderers"]
                     if r["udn"] == "uuid:fake-renderer-0001"],
            30,
        )
        if not check(bool(found), "SSDP 发现 MediaRenderer 并解析设备描述"):
            print("!! 渲染器列表：",
                  json.dumps(http_json(f"{base}/api/renderers"), ensure_ascii=False))
            return 1
        renderer = found[0]
        check(renderer["name"] == "Fake Living Room Speaker",
              "friendlyName 解析正确", renderer["name"])
        check(renderer["model"] == "FakeRenderer-1",
              "modelName 解析正确（相对 controlURL 已解析为绝对）", renderer["model"])

        # ---------------------------------------------------------- 2. 选择设备
        http_json(f"{base}/api/renderer/select", "POST", {"udn": renderer["udn"]})
        selected = wait_for(
            lambda: next((r for r in http_json(f"{base}/api/renderers")["renderers"]
                          if r.get("selected")), None), 10)
        check(bool(selected), "通过 REST API 选中渲染器")

        # 等能力探测（GetProtocolInfo -> protocolInfo）
        caps = wait_for(lambda: (lambda r: r if r.get("capabilities_checked") else None)(
            next((x for x in http_json(f"{base}/api/renderers")["renderers"]
                  if x["udn"] == renderer["udn"]), {})), 15)
        check(bool(caps) and caps.get("stream_kind") == "wav",
              "protocolInfo 能力探测选择 audio/wav",
              str(caps.get("supported_mime") if caps else None))

        rec = http_json(f"http://127.0.0.1:{fake_info['http_port']}/__record")
        check(any(e["action"] == "GetProtocolInfo" for e in rec["events"]),
              "渲染器收到 ConnectionManager#GetProtocolInfo")

        # ------------------------------------------------ 3. 建立 AirPlay 会话
        meta = MetadataWriter(os.path.join(var_dir, "metadata.fifo"))
        meta.ssnc("conn", "192.168.1.77")
        meta.ssnc("snam", "Integration iPhone")
        time.sleep(0.3)
        meta.emit("core", "minm", "Test Song".encode())
        meta.emit("core", "asar", "Test Artist".encode())
        meta.emit("core", "asal", "Test Album".encode())
        # prgr: start/now/end rate  -> 位置 1.0s，时长 180s
        meta.ssnc("prgr", "0/44100/7938000 44100")
        # phbt: rtp/should_be_time_ns（真实时钟，桥接器用它做精确位置推算）
        try:
            mono = time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)
        except (AttributeError, OSError):
            mono = time.monotonic_ns()
        meta.ssnc("phbt", f"44100/{mono}")
        # pvol: dB 值 -15.00 -> 期望映射为 50%
        meta.ssnc("pvol", "-9.00,-9.00,-30.00,0.00")
        time.sleep(0.3)
        # 开始播放（先 pbeg 再喂 PCM，保证不丢数据）
        meta.ssnc("pbeg")

        feeder = PcmFeeder(os.path.join(var_dir, "audio.fifo"))
        feeder.start()

        # ------------------------------------------- 4. 渲染器应开始拉流并播放
        def stream_started():
            rec = http_json(f"http://127.0.0.1:{fake_info['http_port']}/__record")
            return rec if rec.get("stream_bytes_len", 0) > 0 and rec.get("transport_state") == "PLAYING" else None

        rec = wait_for(stream_started, 30)
        if not check(bool(rec), "渲染器收到 SetAVTransportURI + Play 并开始拉流"):
            rec2 = http_json(f"http://127.0.0.1:{fake_info['http_port']}/__record")
            print("   渲染器事件：", json.dumps(actions(rec2), ensure_ascii=False)[:1200])
            print("   桥接器日志尾部：", _tail(os.path.join(var_dir, "bridge.log")))
            return 1

        set_uri = first(rec, "SetAVTransportURI")
        uri = set_uri["args"].get("CurrentURI", "")
        check("/stream/" in uri and uri.endswith(".wav"),
              "流 URI 使用唯一路径且指向实时 WAV 流", uri)
        check(set_uri["args"].get("CurrentURIMetaData", "").startswith("<DIDL-Lite"),
              "SetAVTransportURI 带 DIDL-Lite 元数据")
        didl = set_uri["args"].get("CurrentURIMetaData", "")
        check("Test Song" in didl and "Test Artist" in didl and "Test Album" in didl,
              "DIDL-Lite 含标题/艺术家/专辑")
        check("duration=" in didl and "musicTrack" in didl,
              "已知时长时 DIDL 使用 musicTrack 且带 duration")
        check(any(e["action"] == "Play" for e in rec["events"]), "渲染器收到 AVTransport#Play")

        headers = first(rec, "STREAM_HEADERS")
        check(bool(headers) and headers["args"]["status"] == 200, "音频流 HTTP 200")
        check(bool(headers) and "audio/wav" in headers["args"]["content_type"],
              "音频流 Content-Type = audio/wav", headers["args"]["content_type"] if headers else "")
        check(bool(headers) and headers["args"]["transfer_mode"] == "Streaming",
              "响应带 transferMode.dlna.org: Streaming")
        check(bool(headers) and "DLNA.ORG" in headers["args"]["features"],
              "响应 contentFeatures.dlna.org 含 DLNA 能力串")

        first64 = bytes.fromhex(rec["stream_first_64_hex"])
        check(first64[:4] == b"RIFF" and first64[8:12] == b"WAVE" and b"fmt " in first64,
              "实时流以合法 RIFF/WAVE 头开始", first64[:16].hex())
        # 44 字节头之后的 PCM 应等于我们写入的确定性图案
        check(len(first64) >= 44, "流首包长度足够覆盖 WAV 头")
        check(first64[44:64] == PcmFeeder.PATTERN[:20],
              "WAV 头之后的 PCM 与写入 FIFO 的数据逐字节一致")

        # ------------------------------------------------------ 5. 内部状态同步
        status = http_json(f"{base}/api/status")
        playback = status["playback"]
        check(playback["state"] == "PLAYING", "内部状态 = PLAYING", playback["state"])
        check(playback["title"] == "Test Song" and playback["artist"] == "Test Artist"
              and playback["album"] == "Test Album", "元数据（标题/艺术家/专辑）已同步")
        check(playback["duration_ms"] == 180000, "时长来自 AirPlay prgr",
              str(playback["duration_ms"]))
        check(playback["position_ms"] is not None and playback["position_ms"] > 0,
              "播放位置可推算", str(playback["position_ms"]))
        check(playback["volume"] == 70, "AirPlay -9dB 映射为 DLNA 音量 70%",
              str(playback["volume"]))
        set_volume = first(rec, "SetVolume")
        check(bool(set_volume) and set_volume["args"].get("DesiredVolume") == "70",
              "渲染器收到 SetVolume(70)",
              str(set_volume["args"]) if set_volume else "无")
        check(status["renderer"] is not None and status["renderer"]["online"] is True,
              "渲染器在线状态正确")

        # ------------------------------------------------------------- 6. 暂停
        before_pause = len(all_of(http_json(f"http://127.0.0.1:{fake_info['http_port']}/__record"), "Pause"))
        meta.ssnc("paus")
        wait_for(lambda: len(all_of(http_json(
            f"http://127.0.0.1:{fake_info['http_port']}/__record"), "Pause")) > before_pause, 10)
        rec = http_json(f"http://127.0.0.1:{fake_info['http_port']}/__record")
        check(len(all_of(rec, "Pause")) > before_pause, "暂停：渲染器收到 AVTransport#Pause")
        check(wait_for(lambda: http_json(f"{base}/api/status")["playback"]["state"] == "PAUSED", 8),
              "暂停：内部状态 = PAUSED")

        # ------------------------------------------------------------- 7. 恢复
        before_play = len(all_of(rec, "Play"))
        meta.ssnc("pres")
        wait_for(lambda: len(all_of(http_json(
            f"http://127.0.0.1:{fake_info['http_port']}/__record"), "Play")) > before_play, 10)
        rec = http_json(f"http://127.0.0.1:{fake_info['http_port']}/__record")
        check(len(all_of(rec, "Play")) > before_play, "恢复：渲染器收到 Play")
        check(wait_for(lambda: http_json(f"{base}/api/status")["playback"]["state"] == "PLAYING", 8),
              "恢复：内部状态 = PLAYING")

        # ------------------------------------------------------------- 8. Seek
        old_uri = uri
        before_set = len(all_of(rec, "SetAVTransportURI"))
        meta.ssnc("pfls", "3969000")          # flush 到 90s 处
        meta.ssnc("prgr", "0/3969000/7938000 44100")   # 新位置 90s
        meta.ssnc("phbt", f"3969000/{time.clock_gettime_ns(time.CLOCK_MONOTONIC_RAW)}")
        wait_for(lambda: len(all_of(http_json(
            f"http://127.0.0.1:{fake_info['http_port']}/__record"), "SetAVTransportURI")) > before_set, 15)
        rec = http_json(f"http://127.0.0.1:{fake_info['http_port']}/__record")
        new_uris = [e["args"].get("CurrentURI", "") for e in all_of(rec, "SetAVTransportURI")]
        check(len(new_uris) > before_set, "Seek：重建了 DLNA 会话（新的 SetAVTransportURI）")
        check(new_uris and new_uris[-1] != old_uri,
              "Seek：使用新的唯一流 URI（避免渲染器复用旧连接）",
              f"{old_uri} -> {new_uris[-1] if new_uris else ''}")
        check(any(e["action"] == "Stop" for e in rec["events"]), "Seek：先对渲染器发送 Stop")

        # ------------------------------------------------------------- 9. 结束
        before_stop = len(all_of(rec, "Stop"))
        meta.ssnc("pend")
        wait_for(lambda: len(all_of(http_json(
            f"http://127.0.0.1:{fake_info['http_port']}/__record"), "Stop")) > before_stop, 10)
        rec = http_json(f"http://127.0.0.1:{fake_info['http_port']}/__record")
        check(len(all_of(rec, "Stop")) > before_stop, "结束：渲染器收到 Stop")
        status = http_json(f"{base}/api/status")
        check(status["playback"]["state"] == "STOPPED", "结束：内部状态 = STOPPED",
              status["playback"]["state"])
        check(status["playback"]["title"] == "", "结束：元数据已清空")

        # ------------------------------------------- 10. SOAP 头/体一致性校验
        mismatched = [e for e in rec["events"]
                      if e.get("header_matches_body") is False]
        check(not mismatched, "所有 SOAP 请求的 SOAPACTION 头与 body 动作一致",
              str(mismatched[:3]))

    finally:
        if feeder:
            feeder.stop()
        if meta:
            meta.close()
        for proc in (bridge_proc, fake_proc):
            try:
                proc.send_signal(signal.SIGTERM)
            except Exception:  # noqa: BLE001
                pass
        for proc in (bridge_proc, fake_proc):
            try:
                proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                proc.kill()
        bridge_log.close()
        if not any(not ok for ok, _n, _d in RESULTS):
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"[info] 失败现场保留在 {workdir}")

    passed = sum(1 for ok, _n, _d in RESULTS if ok)
    failed = [name for ok, name, _d in RESULTS if not ok]
    print("\n" + "=" * 72)
    print(f"集成测试结果: {passed}/{len(RESULTS)} 通过")
    if failed:
        print("失败项：")
        for name in failed:
            print(f"  - {name}")
    return 1 if failed else 0


def _tail(path: str, lines: int = 25) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return "".join(handle.readlines()[-lines:])
    except OSError as exc:
        return f"(无法读取 {path}: {exc})"


if __name__ == "__main__":
    sys.exit(main())
