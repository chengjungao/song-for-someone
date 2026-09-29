# -*- coding: utf-8 -*-
"""回环地址感知的代理绕过测试（对应 G 问题）。

背景：``urllib`` 会读环境变量 ``http_proxy`` / ``https_proxy``，装了代理工具
（Clash / v2ray 等）又手动设过这些变量的用户，如果代理没绕过 ``127.0.0.1``，
访问本机 ACE-Step 的请求就会被转发去代理并失败，界面只报「连不上服务」，
把人引向完全错误的排查方向。

本模块验证四件事：

1. 回环判定正确（纯函数，不联网）；
2. 设了「死代理」环境变量时，**回环**请求仍能直连成功（修复前必失败）；
3. **非回环**请求依然照常走代理（行为一字未改）；
4. 顺着 ``AceStepClient`` 的真实调用链走一遍同样成立。

本地起的是 ``127.0.0.1`` 上的临时服务，不访问外网。
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from song_for_someone import net  # noqa: E402
from song_for_someone.client import AceStepClient  # noqa: E402

# 所有可能影响 urllib 出站路由的代理环境变量。
_PROXY_KEYS = (
    "http_proxy", "HTTP_PROXY",
    "https_proxy", "HTTPS_PROXY",
    "all_proxy", "ALL_PROXY",
    "no_proxy", "NO_PROXY",
)


class _JsonHandler(BaseHTTPRequestHandler):
    """返回一个最小 JSON 的本地服务，用于验证回环直连。"""

    def do_GET(self):  # noqa: N802 - http.server 的命名约定
        body = json.dumps({"ok": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音，避免污染测试输出
        pass


class _CaptureHandler(BaseHTTPRequestHandler):
    """扮演「代理」：记下收到的请求行（走代理时是绝对 URL 形式）。"""

    captured: list = []

    def do_GET(self):  # noqa: N802
        type(self).captured.append(self.path)
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestLoopbackDetection(unittest.TestCase):
    """回环地址判定：纯函数，不联网。"""

    def test_true_hosts(self):
        for host in (
            "127.0.0.1", "127.0.0.2", "127.1.2.3",
            "localhost", "LOCALHOST", " 127.0.0.1 ",
            "::1", "[::1]",
        ):
            with self.subTest(host=host):
                self.assertTrue(net.is_loopback_host(host))

    def test_false_hosts(self):
        for host in (
            "", None, "0.0.0.0", "192.168.1.10", "10.0.0.1",
            "example.com", "huggingface.co", "8.8.8.8",
        ):
            with self.subTest(host=host):
                self.assertFalse(net.is_loopback_host(host))

    def test_urls(self):
        self.assertTrue(net.is_loopback_url("http://127.0.0.1:8001/health"))
        self.assertTrue(net.is_loopback_url("http://localhost:8770/"))
        self.assertTrue(net.is_loopback_url("http://[::1]:8001/"))
        self.assertFalse(net.is_loopback_url("http://192.168.1.10:9000/"))
        self.assertFalse(net.is_loopback_url("https://huggingface.co/"))
        self.assertFalse(net.is_loopback_url(""))

    def test_request_objects(self):
        loop = urllib.request.Request("http://127.0.0.1:8001/health")
        remote = urllib.request.Request("http://example.com/")
        self.assertTrue(net.is_loopback_url(loop))
        self.assertFalse(net.is_loopback_url(remote))


class TestProxyBypass(unittest.TestCase):
    """代理绕过：起本地服务 + 操纵代理环境变量实测。"""

    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in _PROXY_KEYS}
        # ``urllib.request.urlopen`` 会惰性构建并缓存一个模块级 opener，其中的
        # ``ProxyHandler`` 在「构建那一刻」就把代理环境变量快照了下来。本测试
        # 会临时改这些变量，若不在收尾时还原这个缓存，就会把错误的代理留给
        # 后面所有用例（真实症状：后续 test_web 的 HTTP 调用全部连接被拒）。
        self._saved_opener = urllib.request._opener
        for key in _PROXY_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        urllib.request._opener = self._saved_opener

    def _start(self, handler) -> int:
        """在 127.0.0.1 的随机端口起服务，返回端口号。"""
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def _stop() -> None:
            # 顺序要紧：先停循环，再等线程退，最后关套接字。
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        self.addCleanup(_stop)
        return server.server_address[1]

    @staticmethod
    def _set_dead_proxy() -> None:
        """设一个没人监听的死代理。"""
        os.environ["http_proxy"] = "http://127.0.0.1:1"
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"

    def test_loopback_bypasses_dead_proxy(self):
        port = self._start(_JsonHandler)
        self._set_dead_proxy()
        # 若被转发到死代理（1 端口）必然连不上；能成功说明绕过了代理。
        with net.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.loads(resp.read().decode("utf-8")), {"ok": True})

    def test_loopback_client_health_with_dead_proxy(self):
        port = self._start(_JsonHandler)
        self._set_dead_proxy()
        client = AceStepClient(f"http://127.0.0.1:{port}")
        self.assertTrue(client.health())

    def test_urlretrieve_loopback_bypasses_dead_proxy(self):
        port = self._start(_JsonHandler)
        self._set_dead_proxy()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.bin"
            net.urlretrieve(f"http://127.0.0.1:{port}/health", target)
            self.assertEqual(target.read_bytes(), b'{"ok": true}')

    def test_non_loopback_still_uses_proxy(self):
        _CaptureHandler.captured = []
        port = self._start(_CaptureHandler)
        os.environ["http_proxy"] = f"http://127.0.0.1:{port}"
        os.environ["HTTP_PROXY"] = f"http://127.0.0.1:{port}"
        # 目标主机不存在也无妨：走代理时请求行是绝对 URL，代理照样能接。
        with net.urlopen("http://example.invalid/probe", timeout=2) as resp:
            self.assertEqual(resp.status, 200)
        self.assertTrue(
            any("example.invalid/probe" in path for path in _CaptureHandler.captured),
            f"代理未收到预期请求，实际收到：{_CaptureHandler.captured}",
        )


if __name__ == "__main__":
    unittest.main()
