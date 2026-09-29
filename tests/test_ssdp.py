"""SSDP 模块单元测试（仅标准库 unittest，可离线运行）。"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from unittest import mock

# 基于 __file__ 稳健地把 app/server 加入 sys.path，使 `air2dlna` 可导入。
REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "app" / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from air2dlna import ssdp  # noqa: E402


def _response(usn: str, location: str, server: str = "", ip: str = "10.0.0.9") -> ssdp.SsdpResponse:
    """构造测试用 SsdpResponse。"""
    return ssdp.SsdpResponse(
        location=location,
        usn=usn,
        st=ssdp.MEDIA_RENDERER_ST,
        server=server,
        ip=ip,
        raw={"location": location, "usn": usn},
    )


class SsdpResponseUdnTests(unittest.TestCase):
    """SsdpResponse.udn 提取。"""

    def test_udn_strips_urn_suffix(self) -> None:
        resp = ssdp.SsdpResponse(
            location="http://192.168.1.10:49152/desc.xml",
            usn="uuid:abc::urn:schemas-upnp-org:device:MediaRenderer:1",
            st=ssdp.MEDIA_RENDERER_ST,
            server="Linux/3.0 UPnP/1.0",
            ip="192.168.1.10",
        )
        self.assertEqual(resp.udn, "uuid:abc")

    def test_udn_without_separator(self) -> None:
        resp = _response("uuid:only-udn", "http://h/d.xml")
        self.assertEqual(resp.udn, "uuid:only-udn")

    def test_udn_strips_only_first_separator(self) -> None:
        resp = _response("uuid:abc::urn:x::urn:y", "http://h/d.xml")
        self.assertEqual(resp.udn, "uuid:abc")

    def test_udn_empty(self) -> None:
        resp = _response("", "http://h/d.xml")
        self.assertEqual(resp.udn, "")

    def test_raw_dict_is_not_shared(self) -> None:
        first = ssdp.SsdpResponse("", "", "", "", "")
        second = ssdp.SsdpResponse("", "", "", "", "")
        first.raw["x"] = "1"
        self.assertEqual(second.raw, {})


class MSearchTests(unittest.TestCase):
    """M-SEARCH 报文构造。"""

    def test_constants(self) -> None:
        self.assertEqual(ssdp.SSDP_ADDR, "239.255.255.250")
        self.assertEqual(ssdp.SSDP_PORT, 1900)
        self.assertEqual(ssdp.MEDIA_RENDERER_ST, "urn:schemas-upnp-org:device:MediaRenderer:1")

    def test_exact_bytes(self) -> None:
        expected = (
            b"M-SEARCH * HTTP/1.1\r\n"
            b"HOST: 239.255.255.250:1900\r\n"
            b'MAN: "ssdp:discover"\r\n'
            b"MX: 2\r\n"
            b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
            b"\r\n"
        )
        self.assertEqual(ssdp.build_m_search(ssdp.MEDIA_RENDERER_ST), expected)

    def test_ssdp_all_and_line_endings(self) -> None:
        message = ssdp.build_m_search("ssdp:all")
        self.assertIn(b"ST: ssdp:all\r\n", message)
        self.assertTrue(message.endswith(b"\r\n\r\n"))
        self.assertNotIn(b"\n\n", message.replace(b"\r\n", b""))

    def test_no_lf_only_lines(self) -> None:
        message = ssdp.build_m_search(ssdp.MEDIA_RENDERER_ST)
        # 每个换行都必须是 CRLF。
        self.assertEqual(message.count(b"\n"), message.count(b"\r\n"))


class ParseResponseTests(unittest.TestCase):
    """SSDP 响应解析的容错性。"""

    def test_parse_crlf_response(self) -> None:
        raw = (
            b"HTTP/1.1 200 OK\r\n"
            b"LOCATION: http://192.168.1.10:49152/desc.xml\r\n"
            b"USN: uuid:abc::urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
            b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
            b"SERVER: Linux/3.0 UPnP/1.0 TestDevice/1.0\r\n"
            b"CACHE-CONTROL: max-age=1800\r\n"
            b"\r\n"
        )
        resp = ssdp.parse_response(raw, "192.168.1.10")
        self.assertIsNotNone(resp)
        assert resp is not None
        self.assertEqual(resp.location, "http://192.168.1.10:49152/desc.xml")
        self.assertEqual(resp.usn, "uuid:abc::urn:schemas-upnp-org:device:MediaRenderer:1")
        self.assertEqual(resp.st, ssdp.MEDIA_RENDERER_ST)
        self.assertEqual(resp.server, "Linux/3.0 UPnP/1.0 TestDevice/1.0")
        self.assertEqual(resp.ip, "192.168.1.10")
        self.assertEqual(resp.udn, "uuid:abc")
        # raw 的键必须是小写。
        self.assertIn("location", resp.raw)
        self.assertEqual(resp.raw["cache-control"], "max-age=1800")

    def test_parse_lf_only_and_case_insensitive_headers(self) -> None:
        raw = (
            b"HTTP/1.1 200 OK\n"
            b"location: http://10.0.0.5/desc.xml\n"
            b"uSn: uuid:xyz::urn:schemas-upnp-org:device:MediaRenderer:1\n"
            b"St: ssdp:all\n"
            b"Server: Test\n"
        )
        resp = ssdp.parse_response(raw, "10.0.0.5")
        self.assertIsNotNone(resp)
        assert resp is not None
        self.assertEqual(resp.location, "http://10.0.0.5/desc.xml")
        self.assertEqual(resp.usn, "uuid:xyz::urn:schemas-upnp-org:device:MediaRenderer:1")
        self.assertEqual(resp.st, "ssdp:all")
        self.assertEqual(resp.udn, "uuid:xyz")

    def test_parse_response_without_location_or_status_line(self) -> None:
        resp = ssdp.parse_response(b"USN: uuid:no-loc\r\nST: ssdp:all\r\n\r\n", "10.0.0.6")
        self.assertIsNotNone(resp)
        assert resp is not None
        self.assertEqual(resp.location, "")
        self.assertEqual(resp.server, "")

    def test_parse_garbage_returns_none(self) -> None:
        self.assertIsNone(ssdp.parse_response(b"", "10.0.0.1"))
        self.assertIsNone(ssdp.parse_response(b"not an ssdp packet at all", "10.0.0.1"))
        self.assertIsNone(ssdp.parse_response(b"\r\n\r\n", "10.0.0.1"))

    def test_parse_tolerates_bad_utf8(self) -> None:
        resp = ssdp.parse_response(b"HTTP/1.1 200 OK\r\nUSN: uuid:\xff\xfe\r\n\r\n", "10.0.0.1")
        self.assertIsNotNone(resp)
        assert resp is not None
        self.assertTrue(resp.usn.startswith("uuid:"))


class DiscoverTests(unittest.TestCase):
    """discover() 的轮次、去重与异常隔离逻辑（全部使用 mock，无真实网络）。"""

    def setUp(self) -> None:
        self.interface = "10.0.0.5"
        self.sleep_patcher = mock.patch.object(ssdp.time, "sleep", return_value=None)
        self.sleep_mock = self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)

    def test_dedup_and_merge_and_search_targets(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_search(interface, st, timeout, log):
            calls.append((interface, st))
            return [
                copy.deepcopy(_response("uuid:aaa", "http://10.0.0.9/desc.xml")),
                copy.deepcopy(
                    _response("uuid:aaa", "http://10.0.0.9/desc.xml", server="Linux/1.0 UPnP/1.0")
                ),
            ]

        with mock.patch.object(ssdp, "_search_once", side_effect=fake_search):
            result = ssdp.discover(timeout=0.05, rounds=3, interfaces=[self.interface])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].udn, "uuid:aaa")
        # 重复项只填补首个非空值。
        self.assertEqual(result[0].server, "Linux/1.0 UPnP/1.0")
        # 第 1 轮只发 MediaRenderer；第 2 轮追加一次 ssdp:all；第 2 轮无新增即提前结束。
        self.assertEqual(
            calls,
            [
                (self.interface, ssdp.MEDIA_RENDERER_ST),
                (self.interface, ssdp.MEDIA_RENDERER_ST),
                (self.interface, ssdp.SSDP_ALL_ST),
            ],
        )
        # 只应在第 1 轮与第 2 轮之间休眠一次。
        self.assertEqual(self.sleep_mock.call_count, 1)

    def test_all_search_never_sent_when_single_round(self) -> None:
        calls: list[str] = []

        def fake_search(interface, st, timeout, log):
            calls.append(st)
            return []

        with mock.patch.object(ssdp, "_search_once", side_effect=fake_search):
            result = ssdp.discover(timeout=0.01, rounds=1, interfaces=[self.interface])

        self.assertEqual(result, [])
        self.assertEqual(calls, [ssdp.MEDIA_RENDERER_ST])
        self.assertEqual(self.sleep_mock.call_count, 0)

    def test_multiple_interfaces_each_searched(self) -> None:
        seen: list[str] = []

        def fake_search(interface, st, timeout, log):
            seen.append(interface)
            return []

        with mock.patch.object(ssdp, "_search_once", side_effect=fake_search):
            ssdp.discover(timeout=0.01, rounds=1, interfaces=["10.0.0.5", "10.0.0.6", "10.0.0.5"])

        self.assertEqual(seen, ["10.0.0.5", "10.0.0.6"])

    def test_empty_interfaces_returns_empty(self) -> None:
        with mock.patch.object(ssdp, "_search_once") as search_mock:
            result = ssdp.discover(timeout=0.01, rounds=1, interfaces=[])
        self.assertEqual(result, [])
        search_mock.assert_not_called()

    def test_socket_failure_is_contained(self) -> None:
        with mock.patch.object(ssdp, "_new_socket", side_effect=OSError("boom")):
            result = ssdp.discover(timeout=0.01, rounds=1, interfaces=["127.0.0.1"])
        self.assertEqual(result, [])

    def test_unexpected_search_exception_is_contained(self) -> None:
        with mock.patch.object(ssdp, "_search_once", side_effect=RuntimeError("kaboom")):
            result = ssdp.discover(timeout=0.01, rounds=1, interfaces=[self.interface])
        self.assertEqual(result, [])

    def test_log_callback_receives_messages(self) -> None:
        messages: list[str] = []
        with mock.patch.object(ssdp, "_search_once", return_value=[]):
            ssdp.discover(timeout=0.01, rounds=1, interfaces=[], log=messages.append)
        self.assertTrue(any("interface" in m or "no usable" in m for m in messages))

    def test_raising_log_callback_is_contained(self) -> None:
        def bad_log(_message: str) -> None:
            raise RuntimeError("logger exploded")

        with mock.patch.object(ssdp, "_search_once", return_value=[]):
            result = ssdp.discover(timeout=0.01, rounds=1, interfaces=[], log=bad_log)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
