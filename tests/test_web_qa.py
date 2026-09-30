# -*- coding: utf-8 -*-
"""Web 化改造的**独立** QA 验收测试（不依赖 ``test_web.py``）。

与 ``test_web.py`` 的区别（刻意为之）：

* 自带一套假客户端 / 假自检，实现细节与工程师那套无关，避免「自己给自己判卷」；
* 侧重**错误路径与边界**：上游不可达、单任务锁、歌词门禁的**服务端**强制、
  路径穿越、非法 JSON、文件名消毒、同分钟重名覆盖、起始脚本端口顺延；
* 会额外跑 **CLI 子命令回归** 与 **依赖/import 红线**的独立核对。

跑法：

    python -m unittest discover -s tests -t . -v
"""

from __future__ import annotations

import ast
import io
import json
import re
import socket
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# tomllib 是 Python 3.11 才进标准库的。本项目承诺零依赖，不能引 tomli 这类
# 回退包，所以 3.9 / 3.10 上退到下面 _array_literal_in() 那个窄解析器。
#
# 别小看这行 import：它一失败，**整个测试模块都收集不到**，本文件 61 条测试
# 会静默消失（3.9 / 3.10 上只剩 279 条），CI 里表现为那两个格子直接红。
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 / 3.10
    tomllib = None

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import song_for_someone.common as common  # noqa: E402
import song_for_someone.web as web  # noqa: E402
from song_for_someone.client import (  # noqa: E402
    AceStepError,
    ServiceUnreachable,
    TaskResult,
)
from song_for_someone.doctor import DoctorReport  # noqa: E402


# ==================================== 跨版本工具（CI 要跑 3.9 ~ 3.12 四个版本）

_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$", re.M)


def _strip_comment(line: str) -> str:
    """去掉 TOML 行末注释，引号里的 # 不算注释起点。"""
    out: List[str] = []
    quote: Optional[str] = None
    for char in line:
        if quote is not None:
            out.append(char)
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
            out.append(char)
        elif char == "#":
            break
        else:
            out.append(char)
    return "".join(out)


def _section_body(text: str, section: str) -> Optional[str]:
    """返回 ``[section]`` 段的正文（到下一个表头为止，已去注释）。

    没有这个段则返回 None —— 让「段不存在」与「段是空的」能分开。
    """
    marks = [
        (m.group(1).strip(), m.start(), m.end())
        for m in _SECTION_RE.finditer(text)
    ]
    for index, (name, _line_start, body_begin) in enumerate(marks):
        if name != section:
            continue
        body_end = marks[index + 1][1] if index + 1 < len(marks) else len(text)
        raw = text[body_begin:body_end]
        return "\n".join(_strip_comment(line) for line in raw.splitlines())
    return None


def _array_literal(text: str, section: str, key: str) -> Optional[str]:
    """取出 ``[section]`` 段里 ``key = [...]`` 的方括号内容，返回去掉空白的原文。

    支持跨行数组。段或键不存在、括号没闭合，都返回 None。

    「读不到」必须和「读到空数组」分开：前者返回 None，断言 ``== ""`` 会失败。
    要是这里把两者混为一谈，窄解析器一旦失手就会把「没找到」当成「是空的」，
    测试变成假绿 —— 那比不测还糟。调用处那句以 keywords 做的自证，就是
    用来堵这个口子的。
    """
    body = _section_body(text, section)
    if body is None:
        return None
    hit = re.search(r"^\s*" + re.escape(key) + r"\s*=\s*\[", body, re.M)
    if hit is None:
        return None
    depth = 0
    for offset in range(hit.end() - 1, len(body)):
        char = body[offset]
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return body[hit.end():offset].strip()
    return None


def _stdlib_names() -> set:
    """标准库顶层模块名集合。

    ``sys.stdlib_module_names`` 是 Python 3.10 才加的，3.9 上访问会
    AttributeError。这里给 3.9 现补一份：标准库目录下的顶层 .py 与包名、
    lib-dynload / DLLs 里的扩展模块名，再并上 C 内建模块名
    （sys、time 这些不落盘，只能从 builtin_module_names 拿到）。

    刻意不写成「3.9 就跳过这条断言」—— 那等于在旧版本上关掉零依赖红线，
    而 3.9 正是 CI 里的一格。
    """
    names = set(sys.builtin_module_names)
    listed = getattr(sys, "stdlib_module_names", None)
    if listed is not None:
        return names | set(listed)

    stdlib = Path(sysconfig.get_paths()["stdlib"])
    for folder in (stdlib, stdlib / "lib-dynload", stdlib.parent / "DLLs"):
        if not folder.is_dir():
            continue
        for entry in folder.iterdir():
            if entry.is_dir():
                names.add(entry.name)
            elif folder.name in ("lib-dynload", "DLLs"):
                # 形如 _socket.cpython-39-x86_64-linux-gnu.so
                names.add(entry.name.split(".")[0])
            elif entry.suffix in (".py", ".pyc"):
                names.add(entry.stem)
    return names


_STDLIB_NAMES = _stdlib_names()

# 一段「干净」歌词：无 error、无 warn（含 [Chorus]，长度够）。
CLEAN = "\n".join([
    "[Verse 1]",
    "那年冬天你把围巾留给我",
    "自己缩着脖子走过三条街",
    "我说你傻你笑着说没事",
    "后来才知道你也怕冷",
    "",
    "[Chorus]",
    "生日快乐 我唱得不算好",
    "但我记得你所有的样子",
    "生日快乐 愿你被世界温柔",
    "像你对我那样温柔",
    "",
    "[Verse 2]",
    "你总说记性不好",
    "却记得我不吃香菜",
    "我讲过的每句话",
    "你都当成正经事听",
    "",
    "[Chorus]",
    "生日快乐 我唱得不算好",
    "但我记得你所有的样子",
    "生日快乐 愿你被世界温柔",
    "像你对我那样温柔",
])

# 只有 error（某行宽度 > 64 列）的歌词。
ERROR_LYRICS = "[Verse 1]\n" + "啊" * 40
# 只有 warn（无结构标签、且太短）的歌词。
WARN_LYRICS = "没有标签的一句歌词随便写下"

# 技术噪音关键词：这些**不许**出现在用户能看到的中文文案里。
NOISE = ("Traceback", "traceback", "URLError", "ConnectionRefused",
         "Exception", "HTTP 4", "HTTP 5")

# QA 客户端只与本机回环服务通信（自己起的临时端口），必须**完全无视**环境
# 变量里的代理配置，也不能复用 ``urllib.request`` 那个会被惰性缓存、可能被
# 其它用例污染的全局 ``_opener``。真实症状：本模块在 discover 里跟在
# ``test_net`` 之后跑时，HTTP 调用全被转发到一个已关闭的代理、连接被拒
# （WinError 10061）。这里显式用一个「对谁都不过代理」的 opener，与产品
# 自身 ``net.urlopen`` 对回环地址的处理保持一致。这一点很关键：装了代理
# 工具并手动设过 ``http_proxy`` 的用户，正是本产品要重点兜住的人群。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _free_port() -> int:
    """要一个当前空闲的 TCP 端口（随即释放，供「不可达」用）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class QAClient:
    """QA 专用假客户端：行为由构造参数决定，绝不联网。"""

    def __init__(self, base_url: str, timeout: float = 60.0, *,
                 health: bool = True,
                 submit_error: Optional[Exception] = None,
                 wait_error: Optional[Exception] = None,
                 results: Optional[List[TaskResult]] = None,
                 latency: float = 0.0) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self._health = health
        self._submit_error = submit_error
        self._wait_error = wait_error
        self._results = results
        self._latency = latency

    def health(self) -> bool:
        return self._health

    def submit(self, request: Any) -> str:
        if self._submit_error is not None:
            raise self._submit_error
        return "qa-upstream-1"

    def wait(self, task_id: str, timeout: float = 1800.0, interval: float = 5.0,
             on_progress: Any = None) -> List[TaskResult]:
        if self._latency:
            time.sleep(self._latency)
        if on_progress:
            on_progress(0.3, 0)
        if self._wait_error is not None:
            raise self._wait_error
        if self._results is not None:
            return self._results
        return [TaskResult(
            file_url="/v1/audio?path=qa",
            metas={"bpm": 77, "keyscale": "D major", "duration": 121.0},
            dit_model="qa-model",
        )]

    def download(self, url: str, out_path: Path) -> Path:
        path = Path(out_path)
        path.write_bytes(b"ID3QA" * 64)
        return path


def _qa_doctor() -> DoctorReport:
    report = DoctorReport()
    report.add("python", "Python 版本", "ok", "QA 环境")
    report.add("service", "ACE-Step 服务", "fail", "端口不通",
               fix="先启动 start_api_server.bat")
    return report


class WebQABase(unittest.TestCase):
    """起真 HTTP 服务（临时端口）+ 注入假依赖。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.songs = Path(cls._tmp.name)
        cls._orig = (web.SONGS_DIR, web._client_factory, web._doctor_runner, web._base_url)

        web.SONGS_DIR = cls.songs
        web._client_factory = cls._factory()
        web._doctor_runner = _qa_doctor

        cls.server, _ = web.pick_server("127.0.0.1", 0, 5)
        if cls.server is None:
            raise RuntimeError("QA 测试服务器起不来")
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def _factory(cls, **kw):
        return lambda base, timeout: QAClient(base, timeout, **kw)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        (web.SONGS_DIR, web._client_factory, web._doctor_runner, web._base_url) = cls._orig
        cls._tmp.cleanup()

    def setUp(self) -> None:
        web.store.reset()
        web.SONGS_DIR = self.songs
        web._client_factory = self._factory()
        web._doctor_runner = _qa_doctor
        web._base_url = "http://127.0.0.1:8001"
        for item in self.songs.iterdir():
            if item.is_file():
                item.unlink()

    # ------------------------------------------------------------ 工具

    def call(self, path: str, method: str = "GET", body: Any = None,
             headers: Optional[Dict[str, str]] = None) -> tuple:
        """发一个请求，返回 (状态码, JSON或bytes, 响应头)。"""
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        head = dict(headers or {})
        if body is not None:
            data = body if isinstance(body, (bytes, str)) else json.dumps(body)
            data = data.encode("utf-8") if isinstance(data, str) else data
            head.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, method=method, headers=head)
        try:
            with _NO_PROXY_OPENER.open(req, timeout=15) as resp:
                return self._parse(resp)
        except urllib.error.HTTPError as exc:
            return self._parse(exc)

    @staticmethod
    def _parse(resp) -> tuple:
        raw = resp.read()
        ctype = resp.headers.get("Content-Type", "")
        if "json" in ctype:
            payload: Any = json.loads(raw.decode("utf-8"))
        else:
            payload = raw
        return resp.status, payload, dict(resp.headers)

    def generate(self, **overrides: Any) -> tuple:
        body = {"prompt": "温暖民谣", "lyrics": CLEAN, "duration": 120}
        body.update(overrides)
        return self.call("/api/generate", "POST", body)

    def wait_done(self, task_id: str, timeout: float = 8.0) -> dict:
        deadline = time.time() + timeout
        data = {}
        while time.time() < deadline:
            _st, payload, _h = self.call(f"/api/task/{task_id}")
            data = payload["data"]
            if data["status"] in ("done", "failed", "timeout"):
                return data
            time.sleep(0.05)
        self.fail(f"任务未在 {timeout}s 内结束：{data}")


# ============================================================ 环境自检

class TestEnvDiagnostics(WebQABase):
    """P0-1：上游不可达时，环境快查必须说人话、不抛技术噪音。"""

    def test_env_unreachable_is_friendly(self):
        web._base_url = f"http://127.0.0.1:{_free_port()}"
        st, payload, _h = self.call("/api/env")
        self.assertEqual(st, 200)
        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertFalse(data["service_ok"])
        self.assertIn("没连上", data["service_detail"])
        blob = json.dumps(payload, ensure_ascii=False)
        for token in NOISE:
            self.assertNotIn(token, blob, f"/api/env 泄露技术噪音：{token}")

    def test_env_reachable_but_still_initializing(self):
        # 端口能连上，但 health 为假 → 只应提示「可能还在初始化」，不算致命。
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            web._base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
            web._client_factory = self._factory(health=False)
            _st, payload, _h = self.call("/api/env")
            data = payload["data"]
            self.assertTrue(data["service_ok"])
            self.assertIn("初始化", data["service_detail"])
        finally:
            listener.close()

    def test_health_does_not_touch_upstream(self):
        def boom(*_a, **_k):
            raise AssertionError("/health 不该去连上游")

        web._client_factory = boom
        web._base_url = f"http://127.0.0.1:{_free_port()}"
        st, payload, _h = self.call("/health")
        self.assertEqual(st, 200)
        self.assertTrue(payload["ok"])

    def test_doctor_failure_is_plain_chinese(self):
        _st, payload, _h = self.call("/api/doctor")
        self.assertTrue(payload["ok"])
        check = next(c for c in payload["data"]["checks"] if c["key"] == "service")
        self.assertEqual(check["level"], "fail")
        self.assertTrue(check["fix"])
        self.assertNotIn("Traceback", json.dumps(check, ensure_ascii=False))


# ============================================================ 生成 / 错误路径

class TestGenerateErrorPaths(WebQABase):

    def test_service_unreachable_maps_to_friendly_error(self):
        web._client_factory = self._factory(submit_error=ServiceUnreachable("连不上（QA）"))
        _st, payload, _h = self.generate()
        data = self.wait_done(payload["data"]["task_id"])
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["error"]["code"], "service_unreachable")
        self.assertIn("没打开", data["error"]["message"])
        for token in NOISE:
            self.assertNotIn(token, data["error"]["message"])

    def test_submit_failure_code(self):
        web._client_factory = self._factory(submit_error=AceStepError("HTTP 500（QA）"))
        _st, payload, _h = self.generate()
        data = self.wait_done(payload["data"]["task_id"])
        self.assertEqual(data["error"]["code"], "submit_failed")

    def test_generate_failure_code(self):
        web._client_factory = self._factory(wait_error=AceStepError("生成失败（QA）"))
        _st, payload, _h = self.generate()
        data = self.wait_done(payload["data"]["task_id"])
        self.assertEqual(data["error"]["code"], "generate_failed")

    def test_timeout_is_reported_as_timeout(self):
        web._client_factory = self._factory(wait_error=AceStepError("等待超时（1800 秒）"))
        _st, payload, _h = self.generate()
        data = self.wait_done(payload["data"]["task_id"])
        self.assertEqual(data["error"]["code"], "timeout")

    def test_unexpected_exception_never_leaks_traceback(self):
        web._client_factory = self._factory(wait_error=RuntimeError("boom-secret"))
        _st, payload, _h = self.generate()
        data = self.wait_done(payload["data"]["task_id"])
        self.assertEqual(data["status"], "failed")
        self.assertNotIn("Traceback", json.dumps(data, ensure_ascii=False))
        # 用户可见文案里不能出现异常类名；原始细节只允许待在 detail。
        self.assertNotIn("RuntimeError", data["error"]["message"])

    def test_unknown_task_is_404(self):
        st, payload, _h = self.call("/api/task/deadbeef")
        self.assertEqual(st, 404)
        self.assertEqual(payload["error"]["code"], "not_found")


# ============================================================ 单任务锁

class TestSingleTaskLock(WebQABase):

    def _start_slow(self) -> str:
        web._client_factory = self._factory(latency=1.2)
        st, payload, _h = self.generate()
        self.assertEqual(st, 202)
        return payload["data"]["task_id"]

    def test_second_request_is_409_busy_immediately(self):
        first = self._start_slow()
        start = time.time()
        st, payload, _h = self.generate(prompt="另一个")
        elapsed = time.time() - start
        self.assertEqual(st, 409)
        self.assertEqual(payload["error"]["code"], "busy")
        self.assertLess(elapsed, 1.0, "409 必须立刻返回，不能静默排队")
        # 409 里要带当前任务的进度信息，前端才能「接上」。
        self.assertEqual(payload["error"]["data"]["task_id"], first)
        for token in NOISE:
            self.assertNotIn(token, json.dumps(payload, ensure_ascii=False))
        self.wait_done(first)

    def test_lock_released_after_success(self):
        first = self._start_slow()
        self.wait_done(first)
        st, payload, _h = self.generate(prompt="再来一首")
        self.assertEqual(st, 202, "上一首结束后锁必须释放")
        self.wait_done(payload["data"]["task_id"])

    def test_lock_released_after_failure(self):
        web._client_factory = self._factory(submit_error=ServiceUnreachable("挂了（QA）"))
        _st, payload, _h = self.generate()
        self.wait_done(payload["data"]["task_id"])
        st, payload2, _h = self.generate()
        self.assertEqual(st, 202, "失败后也必须释放锁，否则永久 busy")
        self.wait_done(payload2["data"]["task_id"])


# ============================================================ 歌词门禁（服务端）

class TestLyricsGateServerSide(WebQABase):
    """门禁必须在**服务端**生效，绕过前端直接打接口也得被拦。"""

    def test_error_lyrics_blocked_without_force(self):
        st, payload, _h = self.generate(lyrics=ERROR_LYRICS)
        self.assertEqual(st, 409)
        self.assertEqual(payload["error"]["code"], "lyrics_error")
        self.assertTrue(payload["error"]["data"]["issues"])

    def test_error_lyrics_pass_with_force(self):
        st, payload, _h = self.generate(lyrics=ERROR_LYRICS, force=True)
        self.assertEqual(st, 202)
        self.wait_done(payload["data"]["task_id"])

    def test_warn_lyrics_blocked_without_yes(self):
        st, payload, _h = self.generate(lyrics=WARN_LYRICS)
        self.assertEqual(st, 409)
        self.assertEqual(payload["error"]["code"], "lyrics_warning")

    def test_warn_lyrics_pass_with_yes(self):
        st, payload, _h = self.generate(lyrics=WARN_LYRICS, yes=True)
        self.assertEqual(st, 202)
        self.wait_done(payload["data"]["task_id"])

    def test_over_max_chars_is_blocked(self):
        huge = "[Verse 1]\n" + ("好" * 4100)
        st, payload, _h = self.generate(lyrics=huge)
        self.assertEqual(st, 409)
        self.assertEqual(payload["error"]["code"], "lyrics_error")
        codes = {i["code"] for i in payload["error"]["data"]["issues"]}
        self.assertIn("too-long", codes)

    def test_empty_and_whitespace_lyrics_rejected(self):
        for bad in ("", "   \n\t  "):
            st, payload, _h = self.generate(lyrics=bad)
            self.assertEqual(st, 400, f"空歌词应被拒：{bad!r}")
            self.assertEqual(payload["error"]["code"], "bad_request")

    def test_missing_prompt_rejected(self):
        st, payload, _h = self.generate(prompt="")
        self.assertEqual(st, 400)

    def test_lyrics_check_endpoint_is_side_effect_free(self):
        # 体检接口用 error 歌词也只应回报告，不应触发任何生成 / 抢锁。
        st, payload, _h = self.call("/api/lyrics/check", "POST", {"lyrics": ERROR_LYRICS})
        self.assertEqual(st, 200)
        self.assertFalse(payload["data"]["ok"])
        self.assertIsNone(web.store.current(), "体检不该占用生成锁")


# ============================================================ 请求体校验

class TestRequestBodyValidation(WebQABase):

    def test_invalid_json_body_is_400(self):
        st, payload, _h = self.call("/api/generate", "POST", b"{not json")
        self.assertEqual(st, 400)
        self.assertEqual(payload["error"]["code"], "bad_request")

    def test_non_object_json_body_is_400(self):
        st, payload, _h = self.call("/api/generate", "POST", "[1, 2, 3]")
        self.assertEqual(st, 400)

    def test_duration_type_error_is_422(self):
        st, payload, _h = self.generate(duration="一会儿")
        self.assertEqual(st, 422)
        self.assertEqual(payload["error"]["code"], "bad_duration")

    def test_duration_range_checked(self):
        for bad in (5, 601):
            st, payload, _h = self.generate(duration=bad)
            self.assertEqual(st, 422, f"越界时长应被拒：{bad}")

    def test_numeric_string_duration_accepted(self):
        st, payload, _h = self.generate(duration="120")
        self.assertEqual(st, 202)
        self.wait_done(payload["data"]["task_id"])

    def test_non_integer_steps_is_400_not_500(self):
        st, payload, _h = self.generate(steps="很多")
        self.assertEqual(st, 400)
        self.assertEqual(payload["error"]["code"], "bad_request")

    def test_versions_clamped_to_max(self):
        st, payload, _h = self.generate(versions=999)
        self.assertEqual(st, 202)
        data = self.wait_done(payload["data"]["task_id"])
        self.assertLessEqual(data["result"]["summary"]["versions"], 8)

    def test_unknown_style_key_does_not_crash(self):
        st, _payload, _h = self.generate(style_key="does-not-exist")
        self.assertEqual(st, 202)

    def test_put_method_is_not_accepted(self):
        st, _payload, _h = self.call("/api/generate", "PUT", {"prompt": "x"})
        self.assertNotEqual(st, 200)
        self.assertIn(st, (404, 405, 501))


# ============================================================ 文件与路径安全

class TestPathAndFileSafety(WebQABase):

    def test_static_path_traversal_not_served(self):
        for path in ("/..%2f..%2fetc%2fpasswd", "/%2e%2e/%2e%2e/win.ini",
                     "/..%5c..%5cwindows%5cwin.ini"):
            st, payload, _h = self.call(path)
            self.assertIn(st, (403, 404), f"{path} 应被拒，实际 {st}")
            if isinstance(payload, dict):
                self.assertFalse(payload.get("ok", False))

    def test_media_rejects_traversal(self):
        for name in ("..%2f..%2fetc%2fpasswd", "sub%2fdir%2fx.mp3", "..%5cwin.ini"):
            st, payload, _h = self.call("/media/" + name)
            self.assertEqual(st, 403, f"/media/{name} 应 403")
            self.assertEqual(payload["error"]["code"], "forbidden")

    def test_download_rejects_traversal(self):
        st, payload, _h = self.call("/download/..%2fsecret")
        self.assertEqual(st, 403)

    def test_missing_media_is_404(self):
        st, _payload, _h = self.call("/media/" + urllib.parse.quote("没有这个.mp3"))
        self.assertEqual(st, 404)

    def test_media_serves_and_supports_range(self):
        _st, payload, _h = self.generate()
        data = self.wait_done(payload["data"]["task_id"])
        name = urllib.parse.quote(data["result"]["files"][0]["name"])
        st, _body, headers = self.call("/media/" + name)
        self.assertEqual(st, 200)
        self.assertEqual(headers.get("Content-Type"), "audio/mpeg")
        st2, body2, headers2 = self.call("/media/" + name, headers={"Range": "bytes=-4"})
        self.assertEqual(st2, 206)
        self.assertTrue(headers2.get("Content-Range", "").startswith("bytes "))
        self.assertEqual(len(body2), 4)

    def test_download_uses_utf8_filename(self):
        _st, payload, _h = self.generate(out_prefix="给妈妈")
        data = self.wait_done(payload["data"]["task_id"])
        name = urllib.parse.quote(data["result"]["files"][0]["name"])
        _st2, _body, headers = self.call("/download/" + name)
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertIn("filename*=UTF-8''", headers.get("Content-Disposition", ""))

    def test_out_prefix_sanitized(self):
        st, payload, _h = self.generate(out_prefix='给妈妈\\/:*?"<>|\x07 测试')
        self.assertEqual(st, 202)
        name = payload["data"]["out_name"]
        for ch in '\\/:*?"<>|':
            self.assertNotIn(ch, name, f"文件名残留非法字符 {ch!r}")
        self.assertIn("给妈妈", name, "中文应被保留")
        self.wait_done(payload["data"]["task_id"])

    def test_out_prefix_windows_reserved_name(self):
        _st, payload, _h = self.generate(out_prefix="CON")
        self.assertTrue(payload["data"]["out_name"].startswith("_CON"))

    def test_out_prefix_truncated_but_name_reasonable(self):
        _st, payload, _h = self.generate(out_prefix="很" * 100)
        name = payload["data"]["out_name"]
        # 前缀被截到 32 个字符以内，加上 "song-MMDD-HHMM" 与扩展名不会离谱地长。
        self.assertLess(len(name), 60)


# ============================================================ 结果契约（前端消费）

class TestResultSummaryContract(WebQABase):
    """完成页「这首歌用到的设置」由 ``result.summary`` 渲染。

    前端 ``app.js`` 读的是**友好键名**（与后端别名表一致）：
    ``fixed_id / tempo / tune / steps / versions / file_type / draft_first``
    外加 ``duration / language_label``。后端必须提供同名键。
    """

    UI_KEYS = ("duration", "language_label", "tempo", "tune",
               "steps", "versions", "file_type", "draft_first", "fixed_id")

    def _summary(self, **overrides: Any) -> dict:
        _st, payload, _h = self.generate(**overrides)
        data = self.wait_done(payload["data"]["task_id"])
        self.assertEqual(data["status"], "done")
        return data["result"]["summary"]

    def test_summary_exposes_every_key_the_ui_reads(self):
        summary = self._summary(tempo=99, steps=12)
        missing = [k for k in self.UI_KEYS if k not in summary]
        self.assertEqual(missing, [], f"完成页读了但后端没给的键：{missing}")

    def test_user_bpm_is_echoed_back_to_ui(self):
        summary = self._summary(tempo=99)
        self.assertEqual(summary.get("tempo"), 99,
                         "用户填了 99 拍/分钟，完成页不该显示「模型自定」")

    def test_user_fixed_id_is_echoed_back(self):
        summary = self._summary(fixed_id=4242)
        self.assertEqual(summary["fixed_id"], 4242)

    def test_reproduce_command_uses_real_cli_params(self):
        _st, payload, _h = self.generate()
        data = self.wait_done(payload["data"]["task_id"])
        cmd = data["result"]["reproduce"]
        self.assertIn("sfs make", cmd)
        self.assertIn("--caption", cmd)
        self.assertIn("--lyrics-file", cmd)
        self.assertIn("--duration", cmd)

    def test_reproduce_includes_seed_when_fixed(self):
        _st, payload, _h = self.generate(fixed_id=777)
        data = self.wait_done(payload["data"]["task_id"])
        self.assertIn("--seed 777", data["result"]["reproduce"])

    def test_public_task_hides_internal_objects(self):
        _st, payload, _h = self.generate()
        _st2, tp, _h = self.call("/api/task/" + payload["data"]["task_id"])
        self.assertNotIn("request_obj", tp["data"])
        self.assertNotIn("cancel_flag", tp["data"])

    def test_elapsed_is_recomputed_not_frozen(self):
        web._client_factory = self._factory(latency=1.0)
        _st, payload, _h = self.generate()
        tid = payload["data"]["task_id"]
        _s, a, _h = self.call(f"/api/task/{tid}")
        time.sleep(0.4)
        _s, b, _h = self.call(f"/api/task/{tid}")
        self.assertGreaterEqual(b["data"]["elapsed"], a["data"]["elapsed"])
        self.wait_done(tid)


class TestFilenameUniqueness(WebQABase):
    """同一分钟内连续出两首，绝不能悄悄覆盖前一首。"""

    def setUp(self) -> None:
        super().setUp()
        self._orig_dt = getattr(common, "datetime", None)

        class FrozenDatetime:
            @classmethod
            def now(cls):
                return datetime(2026, 9, 29, 22, 55)

        common.datetime = FrozenDatetime

    def tearDown(self) -> None:
        if self._orig_dt is not None:
            common.datetime = self._orig_dt

    def test_two_runs_do_not_collide(self):
        names = []
        for _ in range(2):
            _st, payload, _h = self.generate(out_prefix="同一分钟")
            names.append(payload["data"]["out_name"])
            self.wait_done(payload["data"]["task_id"])
        self.assertNotEqual(
            names[0], names[1],
            "同一分钟内两首同名，第二首会静默覆盖第一首（数据丢失）",
        )
        mp3s = [p for p in self.songs.iterdir() if p.suffix == ".mp3"]
        self.assertEqual(len(mp3s), 2, "两首应各自落盘，实际发生了覆盖")


# ============================================================ 代码契约（回归红线）

class TestCodeContracts(unittest.TestCase):

    def test_dependencies_are_empty(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

        if tomllib is not None:
            # 3.11+ 有标准库解析器，用权威结果，再拿字面判据交叉验一遍
            data = tomllib.loads(text)
            self.assertEqual(data["project"]["dependencies"], [])
            self.assertEqual(data["project"]["optional-dependencies"]["dev"], [])
        else:
            # 3.9 / 3.10：先证明窄解析器真的在工作，再断言依赖为空。
            # 少了这一步，解析器失手会返回 None，而成双的空数组断言照样通过。
            # 分两级自证：段定位能用，数组取值也能用。
            self.assertIsNotNone(
                _section_body(text, "project"),
                "连 [project] 段都没定位到，窄解析器失手了",
            )
            keywords = _array_literal(text, "project", "keywords")
            self.assertTrue(
                keywords and "ace-step" in keywords,
                "窄解析器读不出同段里的 keywords，说明它失手了；"
                "这种情况下「dependencies 是空的」不可信，直接判失败",
            )

        # 两条路都要过的字面判据：零依赖必须在这两个方括号里看得见
        self.assertEqual(
            _array_literal(text, "project", "dependencies"), "",
            "project.dependencies 不是空数组（或读不到）—— 本项目承诺零第三方依赖",
        )
        self.assertEqual(
            _array_literal(text, "project.optional-dependencies", "dev"), "",
            "project.optional-dependencies.dev 不是空数组（或读不到）",
        )

    def test_web_imports_no_cli_and_no_third_party(self):
        tree = ast.parse((ROOT / "song_for_someone" / "web.py").read_text(encoding="utf-8"))
        relatives: List[str] = []
        absolutes: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.level == 1:
                    relatives.append(node.module)
            elif isinstance(node, ast.Import):
                absolutes.extend(a.name.split(".")[0] for a in node.names)
        self.assertNotIn("cli", relatives, "web.py 不得反向依赖 cli")
        self.assertNotIn("cli", absolutes)
        third_party = [m for m in absolutes if m not in _STDLIB_NAMES]
        self.assertEqual(third_party, [], f"web.py 引入了第三方依赖：{third_party}")

    def test_common_imports_no_cli(self):
        tree = ast.parse((ROOT / "song_for_someone" / "common.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                self.assertNotEqual(node.module, "cli")
            if isinstance(node, ast.Import):
                self.assertNotIn("cli", [a.name for a in node.names])

    def test_server_binds_loopback_only(self):
        self.assertEqual(web.HOST, "127.0.0.1")

    def test_default_port_constant(self):
        self.assertEqual(web.DEFAULT_PORT, 8770)
        self.assertEqual(web.PORT_RANGE, 30)


# ============================================================ CLI 回归（独立核对返回码）

class TestCliRegression(unittest.TestCase):
    """独立跑 6 个子命令，核对返回码语义（不采信工程师的口头说明）。"""

    def _run(self, args: List[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "song_for_someone", *args],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_doctor_returns_1_when_upstream_down(self):
        # 指向一个必然连不上的地址，保证是「失败」语义。
        proc = self._run(["doctor", "--base-url", f"http://127.0.0.1:{_free_port()}"])
        self.assertEqual(proc.returncode, 1)

    def test_check_ok_returns_0(self):
        f = self.tmp / "ok.txt"
        f.write_text(CLEAN, encoding="utf-8")
        self.assertEqual(self._run(["check", str(f)]).returncode, 0)

    def test_check_error_returns_1(self):
        f = self.tmp / "bad.txt"
        f.write_text(ERROR_LYRICS, encoding="utf-8")
        self.assertEqual(self._run(["check", str(f)]).returncode, 1)

    def test_styles_and_style_subcommands(self):
        self.assertEqual(self._run(["styles"]).returncode, 0)
        self.assertEqual(self._run(["style", "folk"]).returncode, 0)
        self.assertEqual(self._run(["style", "nope"]).returncode, 2)

    def test_make_dry_run_blocked_by_warn_returns_3(self):
        f = self.tmp / "warn.txt"
        f.write_text(WARN_LYRICS, encoding="utf-8")
        proc = self._run(["make", "--style", "folk", "--lyrics-file", str(f), "--dry-run"])
        self.assertEqual(proc.returncode, 3)

    def test_make_dry_run_ok_returns_0(self):
        f = self.tmp / "ok.txt"
        f.write_text(CLEAN, encoding="utf-8")
        proc = self._run(["make", "--style", "folk", "--lyrics-file", str(f),
                          "--dry-run", "--out-dir", str(self.tmp)])
        self.assertEqual(proc.returncode, 0)

    def test_songs_subcommand_returns_0(self):
        self.assertEqual(self._run(["songs", "--out-dir", str(self.tmp)]).returncode, 0)


# ============================================================ 启动脚本

class TestStartScript(unittest.TestCase):

    def test_old_python_branch_is_simulated(self):
        import start as start_mod

        class FakeVersion(tuple):
            major = 3
            minor = 8
            micro = 0
            releaselevel = "final"
            serial = 0

        class FakeSys:
            version_info = FakeVersion((3, 8, 0, "final", 0))

        orig = start_mod.sys
        orig_pause = start_mod._pause
        start_mod.sys = FakeSys()  # type: ignore[assignment]
        start_mod._pause = lambda: None  # 双击时的「按回车关闭」不该在测试里等输入
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = start_mod.main()
        finally:
            start_mod.sys = orig
            start_mod._pause = orig_pause
        self.assertEqual(rc, 1)
        out = buf.getvalue()
        self.assertIn("3.9", out)
        self.assertIn("python.org", out)

    def test_pick_server_skips_busy_port(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy = blocker.getsockname()[1]
        try:
            server, chosen = web.pick_server("127.0.0.1", busy, 5)
            self.assertIsNotNone(server)
            self.assertNotEqual(chosen, busy)
            server.server_close()
        finally:
            blocker.close()

    def test_start_py_real_process_falls_back_and_releases_port(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy = blocker.getsockname()[1]

        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "start.py"),
             # --no-engine 是必须的：本用例只验证「端口顺延 + 真的监听起来」，
             # 不传这个参数 start.py 会去真拉 ACE-Step 引擎（十几分钟、占满显存），
             # 测试不该有这种副作用。引擎编排本身由 tests/test_engine.py 覆盖。
             "--no-engine", "--no-browser", "--port", str(busy)],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        lines: List[str] = []

        def pump() -> None:
            for line in proc.stdout:  # type: ignore[union-attr]
                lines.append(line)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        try:
            deadline = time.time() + 30
            while time.time() < deadline:
                if any("界面已启动" in ln for ln in lines):
                    break
                time.sleep(0.1)
            banner = "".join(lines)
            self.assertIn("界面已启动", banner, f"start.py 未打印启动横幅：{banner!r}")
            actual = int(banner.split("127.0.0.1:")[1].split("/")[0])
            self.assertNotEqual(actual, busy, "端口被占用时应顺延")
            self.assertEqual(actual, busy + 1)
            # 顺延后的端口确实在监听
            with socket.create_connection(("127.0.0.1", actual), timeout=3):
                pass
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            if proc.stdout is not None:
                proc.stdout.close()
            blocker.close()

        # 关掉之后端口应被释放（无孤儿进程占着）
        time.sleep(0.5)
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", actual))
            released = True
        except OSError:
            released = False
        finally:
            probe.close()
        self.assertTrue(released, f"退出后端口 {actual} 仍被占用（可能有孤儿进程）")


if __name__ == "__main__":
    unittest.main()
