"""HTTP 层：Web UI 静态资源、REST API、实时 PCM/WAV 流、GENA 回调。

对应 TECHNICAL_DESIGN 第 6、10、16、18 节。

安全约定（第 18 节）：

* 静态文件只能来自 ``app/ui`` 目录，且经过真实路径规范化校验，拒绝目录穿越；
* 所有写操作（PUT/POST）必须带 ``X-Requested-With`` 头，作为 CSRF 防护；
* 不使用任何 shell，不在响应中回显敏感信息；
* 每个请求都有长度上限与超时。
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import posixpath
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

log = logging.getLogger("webui")

MAX_BODY_BYTES = 64 * 1024
_ALLOWED_STATIC_EXT = {
    ".html", ".css", ".js", ".mjs", ".png", ".jpg", ".jpeg",
    ".svg", ".ico", ".webmanifest", ".json", ".woff2", ".map", ".txt",
}


class AppContext:
    """HTTP 层需要的全部依赖，避免全局变量。"""

    def __init__(self, config, registry, controller, streams, log_path: str,
                 version: str, ui_dir: str, started_at: float) -> None:
        self.config = config
        self.registry = registry
        self.controller = controller
        self.streams = streams
        self.log_path = log_path
        self.version = version
        self.ui_dir = ui_dir
        self.started_at = started_at
        self.discovering = threading.Event()
        self.airplay_supervisor = None      # 由 bridge.py 注入
        self.nqptp_supervisor = None        # 由 bridge.py 注入（若由本进程监管）
        self.shairport_name_hint = ""


class Handler(BaseHTTPRequestHandler):
    server_version = "AirPlay2DLNA/1.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ 基础
    @property
    def ctx(self) -> AppContext:
        return self.server.ctx  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, payload, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json({"ok": False, "error": message}, status=status)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            raise ValueError("请求体过大")
        return self.rfile.read(length)

    def _read_json_body(self) -> Optional[dict]:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON: {exc}") from None
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _check_csrf(self) -> bool:
        """写操作必须带自定义头，防止跨站表单提交。"""
        if (self.headers.get("X-Requested-With") or "").lower() == "xmlhttprequest":
            return True
        self._send_error_json(HTTPStatus.FORBIDDEN, "缺少 X-Requested-With 头（CSRF 防护）")
        return False

    def _config_payload(self) -> dict:
        payload = self.ctx.config.snapshot()
        # 只暴露 UI 需要的字段
        return {
            "airplay_name": payload.get("airplay_name", ""),
            "selected_renderer_udn": payload.get("selected_renderer_udn", ""),
            "selected_renderer_name": payload.get("selected_renderer_name", ""),
            "selected_renderer_ip": payload.get("selected_renderer_ip", ""),
            "log_level": payload.get("log_level", "info"),
            "rtsp_port": payload.get("rtsp_port"),
            "http_port": payload.get("http_port"),
            "preroll_seconds": payload.get("preroll_seconds"),
            "buffer_seconds": payload.get("buffer_seconds"),
            "av_offset_ms": payload.get("av_offset_ms"),
        }

    # ------------------------------------------------------------------ 路由
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_NOTIFY(self) -> None:  # noqa: N802 - GENA 回调使用 NOTIFY 方法
        self._dispatch("NOTIFY")

    def _dispatch(self, method: str) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                self._handle_api(method, path, query)
            elif path.startswith("/stream/"):
                self._handle_stream(method, path)
            elif path.startswith("/upnp/notify/"):
                self._handle_notify(path)
            elif method in ("GET", "HEAD"):
                self._handle_static(path)
            else:
                self._send_error_json(HTTPStatus.METHOD_NOT_ALLOWED, "不支持的方法")
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001 - HTTP 线程绝不能因单个请求崩溃
            log.exception("处理请求失败: %s %s", method, path)
            try:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "内部错误")
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ API
    def _handle_api(self, method: str, path: str, query: dict) -> None:
        ctx = self.ctx
        if path == "/api/health" and method in ("GET", "HEAD"):
            self._send_json({"ok": True})
            return

        if path == "/api/status" and method in ("GET", "HEAD"):
            self._send_json(self._build_status())
            return

        if path == "/api/config":
            if method in ("GET", "HEAD"):
                self._send_json(self._config_payload())
                return
            if method == "PUT":
                if not self._check_csrf():
                    return
                try:
                    changes = self._read_json_body() or {}
                    ctx.config.update(changes)
                except ValueError as exc:
                    self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                except Exception as exc:  # noqa: BLE001
                    self._send_error_json(HTTPStatus.BAD_REQUEST, f"配置保存失败: {exc}")
                    return
                if "log_level" in changes:
                    from . import logging_setup

                    logging_setup.set_level(changes["log_level"])
                self._send_json({"ok": True, "config": self._config_payload()})
                return

        if path == "/api/renderers" and method in ("GET", "HEAD"):
            self._send_json({
                "discovering": ctx.registry.discovering,
                "renderers": ctx.registry.list(),
            })
            return

        if path == "/api/renderers/discover" and method == "POST":
            if not self._check_csrf():
                return
            self._trigger_discovery()
            self._send_json({"ok": True, "discovering": True})
            return

        if path == "/api/renderer/select" and method == "POST":
            if not self._check_csrf():
                return
            try:
                body = self._read_json_body() or {}
            except ValueError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return
            udn = str(body.get("udn", "")).strip()
            if not udn:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "缺少 udn")
                return
            try:
                ctx.controller.set_renderer(udn)
            except KeyError as exc:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                self._send_error_json(HTTPStatus.BAD_REQUEST, f"选择设备失败: {exc}")
                return
            self._send_json({"ok": True, "renderer": next(
                (r for r in ctx.registry.list() if r["udn"] == udn), None
            )})
            return

        if path == "/api/logs" and method in ("GET", "HEAD"):
            try:
                lines = int((query.get("lines") or ["200"])[0])
            except ValueError:
                lines = 200
            lines = max(1, min(lines, 2000))
            self._send_json({"lines": self._read_logs(lines)})
            return

        if path == "/api/artwork" and method in ("GET", "HEAD"):
            self._handle_artwork()
            return

        if path == "/api/timeline" and method in ("GET", "HEAD"):
            self._send_json({
                "timeline": ctx.controller.timeline.snapshot(),
                "buffer": ctx.controller.ring.stats(),
            })
            return

        self._send_error_json(HTTPStatus.NOT_FOUND, f"未知接口: {path}")

    def _build_status(self) -> dict:
        import time

        from . import netif

        ctx = self.ctx
        base = ctx.controller.status()
        config = ctx.config.snapshot()

        airplay_sup = ctx.airplay_supervisor.status() if ctx.airplay_supervisor else {}
        nqptp_sup = ctx.nqptp_supervisor.status() if ctx.nqptp_supervisor else {}
        running = bool(airplay_sup.get("running"))

        session = base["playback"].get("volume")
        client = {}
        raw_session = ctx.controller.state.airplay_session
        if raw_session:
            client = {
                "connected": bool(raw_session.get("client_ip")),
                "name": raw_session.get("name") or "",
                "ip": raw_session.get("client_ip") or "",
            }

        return {
            "airplay": {
                "running": running,
                "name": config.get("airplay_name", ""),
                "rtsp_port": config.get("rtsp_port"),
                "mdns": "registered" if running else "pending",
                "client": client or {"connected": False, "name": "", "ip": ""},
                "process": airplay_sup,
                "nqptp": nqptp_sup,
            },
            "renderer": base["renderer"],
            "playback": base["playback"],
            "server": {
                "http_port": config.get("http_port"),
                "nas_ip": netif.primary_lan_address(),
                "version": ctx.version,
                "uptime_s": int(time.monotonic() - ctx.started_at),
            },
            "timeline": base["timeline"],
            "buffer": base["buffer"],
            "volume": session,
        }

    def _trigger_discovery(self) -> None:
        ctx = self.ctx
        if ctx.discovering.is_set():
            return
        ctx.discovering.set()

        def _run() -> None:
            try:
                ctx.registry.scan(timeout=2.5, rounds=3, deep=False)
            except Exception:  # noqa: BLE001
                log.exception("手动搜索设备失败")
            finally:
                ctx.discovering.clear()

        threading.Thread(target=_run, name="manual-discovery", daemon=True).start()

    def _read_logs(self, lines: int) -> list[str]:
        from .logging_setup import get_ring_handler

        ring = get_ring_handler().tail(lines)
        if ring:
            return ring
        # 内存环形缓冲为空（例如刚启动）时读文件
        path = self.ctx.log_path
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                from collections import deque

                return list(deque(handle, maxlen=lines))
        except OSError as exc:
            return [f"无法读取日志文件 {path}: {exc}"]

    def _handle_artwork(self) -> None:
        data, mime = self.ctx.controller.artwork()
        if not data:
            self._send_error_json(HTTPStatus.NOT_FOUND, "当前没有封面图")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # ------------------------------------------------------------------ 流
    def _handle_stream(self, method: str, path: str) -> None:
        if method not in ("GET", "HEAD"):
            self._send_error_json(HTTPStatus.METHOD_NOT_ALLOWED, "不支持的方法")
            return
        token = posixpath.basename(path).split(".")[0]
        session = self.ctx.streams.get(token)
        if session is None:
            log.info("渲染器请求了未知的流 token=%s（可能已换届）", token)
            self._send_error_json(HTTPStatus.NOT_FOUND, "流已失效，请重新选择设备或重新播放")
            return

        range_start = 0
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            spec = range_header[len("bytes="):].split(",")[0].strip()
            start_text = spec.split("-")[0]
            try:
                range_start = int(start_text) if start_text else 0
            except ValueError:
                range_start = 0

        head_only = method == "HEAD"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", session.content_type)
        if session.total_bytes is not None and session.kind == "wav":
            # WAV：Content-Length 包含 44 字节头
            self.send_header("Content-Length", str(44 + session.total_bytes))
        elif session.total_bytes is not None:
            self.send_header("Content-Length", str(session.total_bytes))
        else:
            # 长度未知：明确声明流式并关闭连接（兼容性最好的做法）
            self.send_header("Connection", "close")
            self.close_connection = True
        self.send_header("Accept-Ranges", "none")
        self.send_header("transferMode.dlna.org", "Streaming")
        if self.headers.get("getcontentFeatures.dlna.org"):
            self.send_header("contentFeatures.dlna.org", session.protocol_info)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        connection_holder = {"sock": None}
        try:
            connection_holder["sock"] = self.connection
            self.ctx.streams.register_connection(_ConnectionCloser(self.connection))
            self.ctx.streams.serve(
                session, self.wfile, head_only=head_only, range_start=range_start,
            )
        finally:
            pass

    # ------------------------------------------------------------- GENA 回调
    def _handle_notify(self, path: str) -> None:
        token = path.rsplit("/", 1)[-1]
        try:
            body = self._read_body()
        except ValueError as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        try:
            self.ctx.controller.on_notify(token, body)
        except Exception:  # noqa: BLE001
            log.exception("处理 GENA 通知失败")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------- 静态资源
    def _handle_static(self, path: str) -> None:
        ui_dir = os.path.realpath(self.ctx.ui_dir)
        relative = path.lstrip("/") or "index.html"
        candidate = os.path.realpath(os.path.join(ui_dir, relative))
        # 目录穿越防护：必须仍在 ui_dir 之内
        if candidate != ui_dir and not candidate.startswith(ui_dir + os.sep):
            self._send_error_json(HTTPStatus.FORBIDDEN, "非法路径")
            return
        if os.path.isdir(candidate):
            candidate = os.path.join(candidate, "index.html")
        if not os.path.isfile(candidate):
            self._send_error_json(HTTPStatus.NOT_FOUND, "资源不存在")
            return
        extension = os.path.splitext(candidate)[1].lower()
        if extension not in _ALLOWED_STATIC_EXT:
            self._send_error_json(HTTPStatus.FORBIDDEN, "不允许的资源类型")
            return
        mime, _encoding = mimetypes.guess_type(candidate)
        try:
            with open(candidate, "rb") as handle:
                body = handle.read()
        except OSError as exc:
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"读取失败: {exc}")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if extension in (".html",):
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class _ConnectionCloser:
    """可被流管理器强制关闭的连接句柄。"""

    def __init__(self, sock) -> None:
        self._sock = sock

    def close(self) -> None:
        try:
            self._sock.shutdown(2)  # SHUT_RDWR
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


class WebServer:
    """封装 :class:`ThreadingHTTPServer` 的启停。"""

    def __init__(self, host: str, port: int, ctx: AppContext) -> None:
        self.host = host
        self.port = port
        self.ctx = ctx
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self) -> tuple[str, int]:
        if self._httpd is None:
            return self.host, self.port
        return self._httpd.server_address[0], self._httpd.server_address[1]

    def start(self) -> None:
        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        self._httpd.ctx = self.ctx  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="http-server", daemon=True)
        self._thread.start()
        log.info("Web UI / REST API 已监听 %s:%s", self.host, self.port)

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
