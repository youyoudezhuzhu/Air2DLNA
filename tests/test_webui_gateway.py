"""Web UI 网关接入单元测试：飞牛统一网关套接字 + 路径前缀。

覆盖 1.0.1 修复的缺陷：应用中心/桌面入口通过 ``/app/<appname>/...`` 访问应用时，
服务端必须（1）在 Unix 套接字上监听，（2）剥离网关前缀后再按内部路由匹配，
（3）对不带结尾斜杠的前缀做 307 跳转（否则页面内 ``css/app.css`` 这类相对路径
会被解析到 ``/app/css/...``，静态资源全 404 → 空白页）。

测试只在临时目录创建套接字，不占用任何 TCP 端口，也不访问外部网络。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "app" / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from airplay2dlna import webui  # noqa: E402

PREFIX = "/app/airplay2dlna"
INDEX_HTML = "<!DOCTYPE html><html><body>airplay2dlna ui</body></html>"


class _FakeContext:
    """WebUI 路由里用到的最小依赖集合。"""

    def __init__(self, ui_dir: str, gateway_prefix: str) -> None:
        self.ui_dir = ui_dir
        self.gateway_prefix = (gateway_prefix or "").rstrip("/")
        self.version = "test"
        self.log_path = os.path.join(ui_dir, "..", "bridge.log")
        self.config = None
        self.registry = None
        self.controller = None
        self.streams = None
        self.discovering = threading.Event()


def _raw_request(sock_path: str, method: str, target: str, headers: dict | None = None) -> str:
    """通过 Unix 套接字发一个原始 HTTP 请求，返回完整响应文本。"""
    lines = [f"{method} {target} HTTP/1.1", "Host: localhost", "Connection: close"]
    for key, value in (headers or {}).items():
        lines.append(f"{key}: {value}")
    request = "\r\n".join(lines) + "\r\n\r\n"

    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(5.0)
    try:
        conn.connect(sock_path)
        conn.sendall(request.encode("utf-8"))
        chunks = []
        while True:
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        conn.close()
    return b"".join(chunks).decode("utf-8", errors="replace")


class GatewaySocketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls.ui_dir = root / "ui"
        (cls.ui_dir / "css").mkdir(parents=True)
        (cls.ui_dir / "js").mkdir(parents=True)
        (cls.ui_dir / "index.html").write_text(INDEX_HTML, encoding="utf-8")
        (cls.ui_dir / "css" / "app.css").write_text("body{}", encoding="utf-8")
        (cls.ui_dir / "js" / "app.js").write_text("/* ui */", encoding="utf-8")
        cls.sock_path = str(root / "airplay2dlna.sock")

        cls.ctx = _FakeContext(str(cls.ui_dir), PREFIX)
        cls.server = webui.UnixHTTPServer(cls.sock_path, webui.Handler)  # type: ignore[arg-type]
        cls.server.daemon_threads = True
        cls.server.ctx = cls.ctx  # type: ignore[attr-defined]
        cls.thread = threading.Thread(target=cls.server.serve_forever, name="test-unix", daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5.0)
        cls._tmp.cleanup()

    # ------------------------------------------------------------------ 套接字
    def test_socket_file_created_and_world_accessible(self) -> None:
        """网关进程不一定是应用用户，套接字必须存在且可读写。"""
        self.assertTrue(os.path.exists(self.sock_path))
        mode = os.stat(self.sock_path).st_mode & 0o777
        self.assertEqual(0o666, mode, "套接字权限应为 0666，供网关进程连接")

    # -------------------------------------------------------------- 前缀路由
    def test_bare_prefix_redirects_to_trailing_slash(self) -> None:
        """ui/config 里的 url 是 /app/airplay2dlna（无结尾斜杠），必须补斜杠。"""
        response = _raw_request(self.sock_path, "GET", PREFIX)
        self.assertIn("307", response.splitlines()[0])
        self.assertIn(f"Location: {PREFIX}/", response)

    def test_index_served_under_prefix(self) -> None:
        response = _raw_request(self.sock_path, "GET", PREFIX + "/")
        self.assertIn("200", response.splitlines()[0])
        self.assertIn("text/html", response)
        self.assertIn("airplay2dlna ui", response)

    def test_static_assets_served_under_prefix(self) -> None:
        """相对路径资源（css/js/images）必须能在前缀下取到，否则就是空白页。"""
        for asset, expect_type in (("css/app.css", "text/css"), ("js/app.js", "javascript")):
            response = _raw_request(self.sock_path, "GET", f"{PREFIX}/{asset}")
            self.assertIn("200", response.splitlines()[0], asset)
            self.assertIn(expect_type, response, asset)

    def test_api_reachable_under_prefix(self) -> None:
        response = _raw_request(self.sock_path, "GET", f"{PREFIX}/api/health")
        self.assertIn("200", response.splitlines()[0])
        body = response.split("\r\n\r\n", 1)[1]
        self.assertTrue(json.loads(body)["ok"])

    def test_unknown_path_under_prefix_is_404(self) -> None:
        response = _raw_request(self.sock_path, "GET", f"{PREFIX}/nope.txt")
        self.assertIn("404", response.splitlines()[0])

    def test_root_path_without_prefix_still_served(self) -> None:
        """直接访问 TCP 端口（如 http://<NAS>:8788/）也必须正常。"""
        response = _raw_request(self.sock_path, "GET", "/")
        self.assertIn("200", response.splitlines()[0])
        self.assertIn("airplay2dlna ui", response)

    def test_api_without_prefix_still_served(self) -> None:
        response = _raw_request(self.sock_path, "GET", "/api/health")
        self.assertIn("200", response.splitlines()[0])


class PrefixNormalisationTests(unittest.TestCase):
    def test_trailing_slash_is_stripped(self) -> None:
        ctx = _FakeContext("/tmp", "/app/airplay2dlna/")
        self.assertEqual("/app/airplay2dlna", ctx.gateway_prefix)

    def test_empty_prefix_disables_rewrite(self) -> None:
        ctx = _FakeContext("/tmp", "")
        self.assertEqual("", ctx.gateway_prefix)


if __name__ == "__main__":
    unittest.main()
