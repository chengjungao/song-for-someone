# -*- coding: utf-8 -*-
"""网页服务（web.py）的接口测试。

只用标准库 unittest，不联网、不碰 GPU、不依赖真实的 ACE-Step 服务：

* 服务器起在临时端口（``port=0``）；
* 客户端换成假客户端；
* 出歌目录换到临时目录，绝不写进仓库的 ``songs/``；
* 每个用例开始前 ``web.store.reset()``，避免相互污染。

跑法：

    python -m unittest discover -s tests -t . -v
"""

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import song_for_someone.web as web  # noqa: E402
from song_for_someone.client import (  # noqa: E402
    AceStepError,
    GenerateRequest,
    ServiceUnreachable,
    TaskResult,
)
from song_for_someone.common import sanitize_filename_part  # noqa: E402
from song_for_someone.doctor import DoctorReport  # noqa: E402

# 一段「干净」的歌词：analyze() 不会报 error、也不会报 warn。
CLEAN_LYRICS = "\n".join([
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

ERROR_LYRICS = "[Verse 1]\n" + "啊" * 40          # 行太长 → error
WARNING_LYRICS = "没有标签的一句歌词，随便写下"       # 没有结构标签 → warn


def _fake_doctor() -> DoctorReport:
    """不跑真实自检的假报告。"""
    report = DoctorReport()
    report.add("python", "Python 版本", "ok", "3.13 测试环境")
    report.add("service", "ACE-Step 服务", "fail", "端口不通", fix="先启动 start_api_server.bat")
    return report


class _FakeClient:
    """假客户端。行为由构造参数决定，不碰任何真实网络。"""

    def __init__(self, base_url, timeout=60.0, *, fail=None, results=None):
        self.base_url = base_url
        self.timeout = timeout
        self._fail = fail
        self._results = results

    def health(self) -> bool:
        return self._fail != "unreachable"

    def submit(self, request):
        if self._fail == "unreachable":
            raise ServiceUnreachable("连不上 http://127.0.0.1:8001（测试）")
        if self._fail == "submit":
            raise AceStepError("提交失败（测试）")
        return "fake-upstream-task"

    def wait(self, task_id, timeout=1800.0, interval=5.0, on_progress=None):
        if on_progress:
            on_progress(0.5, 0)
        if self._fail == "generate":
            raise AceStepError("生成失败：status=2（测试）")
        if self._fail == "timeout":
            raise AceStepError("等待超时（1800 秒），task_id=fake（测试）")
        if self._results is not None:
            return self._results
        return [
            TaskResult(
                file_url="/v1/audio?path=x",
                metas={"bpm": 77, "keyscale": "D major", "duration": 120.0},
                dit_model="acestep-v15-turbo",
            )
        ]

    def download(self, url, out_path):
        path = Path(out_path)
        path.write_bytes(b"ID3fake-audio-bytes")
        return path


class WebTestCase(unittest.TestCase):
    """所有网页测试的基类：起临时端口、注入假依赖。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.songs_dir = Path(cls._tmp.name)

        cls._orig_songs_dir = web.SONGS_DIR
        cls._orig_factory = web._client_factory
        cls._orig_doctor = web._doctor_runner

        web.SONGS_DIR = cls.songs_dir
        web._client_factory = lambda base, timeout: _FakeClient(base, timeout)
        web._doctor_runner = _fake_doctor

        cls.server, _port = web.pick_server("127.0.0.1", 0, 5)
        if cls.server is None:
            raise RuntimeError("测试服务器起不来")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        web.SONGS_DIR = cls._orig_songs_dir
        web._client_factory = cls._orig_factory
        web._doctor_runner = cls._orig_doctor
        cls._tmp.cleanup()

    def setUp(self):
        web.store.reset()
        web.SONGS_DIR = self.songs_dir
        web._client_factory = lambda base, timeout: _FakeClient(base, timeout)
        web._doctor_runner = _fake_doctor
        # 清掉上一次用例留下的产物
        for item in self.songs_dir.iterdir():
            if item.is_file():
                item.unlink()

    # ---------------------------------------------------------- 辅助

    def call(self, path, method="GET", body=None, headers=None):
        """发一个请求，返回 (status, payload, headers)。

        payload 对 JSON 响应是 dict；对音频等二进制响应是 bytes。
        """
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        head = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            head["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=head)
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                return self._parse(resp)
        except urllib.error.HTTPError as exc:
            return self._parse(exc)

    @staticmethod
    def _parse(response):
        raw = response.read()
        headers = dict(response.headers)
        ctype = response.headers.get("Content-Type", "")
        if "json" in ctype:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                payload = {"ok": False, "error": {"code": "non_json", "message": "bad json"}}
        else:
            payload = raw
        return response.status, payload, headers

    def set_client(self, **kwargs):
        web._client_factory = lambda base, timeout: _FakeClient(base, timeout, **kwargs)

    def wait_done(self, task_id, timeout=5.0):
        deadline = time.time() + timeout
        payload = None
        while time.time() < deadline:
            _status, payload, _headers = self.call(f"/api/task/{task_id}")
            state = payload["data"]["status"]
            if state in ("done", "failed", "timeout"):
                return payload["data"]
            time.sleep(0.05)
        self.fail(f"任务没有在 {timeout} 秒内结束：{payload}")


class TestHealthAndMeta(WebTestCase):
    def test_health_is_alive_and_200(self):
        status, payload, _headers = self.call("/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["app"], "song-for-someone")
        self.assertEqual(payload["data"]["version"], "0.2.0")

    def test_meta_has_ten_styles(self):
        _status, payload, _headers = self.call("/api/meta")
        self.assertTrue(payload["ok"])
        styles = payload["data"]["styles"]
        self.assertEqual(len(styles), 10)
        for style in styles:
            self.assertIn("prompt", style)
            self.assertIn(style["badge"], ("实测", "建议"))
        self.assertEqual(payload["data"]["lyrics_max_chars"], 4096)

    def test_meta_durations_cover_all_templates(self):
        _status, payload, _headers = self.call("/api/meta")
        durations = payload["data"]["durations"]
        for value in (60, 90, 100, 120, 150, 180):
            self.assertIn(value, durations)
        for style in payload["data"]["styles"]:
            self.assertIn(int(style["duration"]), durations)

    def test_env_reports_shape(self):
        _status, payload, _headers = self.call("/api/env")
        self.assertTrue(payload["ok"])
        data = payload["data"]
        for key in ("python_ok", "service_ok", "service_detail", "out_dir_ok"):
            self.assertIn(key, data)
        self.assertTrue(data["out_dir_ok"])

    def test_env_exposes_three_way_level(self):
        # 三态角标靠这个字段，缺了它红色「不可用」永远出不来。
        _status, payload, _headers = self.call("/api/env")
        self.assertIn(payload["data"]["level"], ("ok", "warn", "bad"))

    def test_example_endpoint(self):
        _status, payload, _headers = self.call("/api/example?name=birthday")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["name"], "birthday")
        self.assertTrue(payload["data"]["text"])

    def test_example_unknown_name_404(self):
        status, payload, _headers = self.call("/api/example?name=nope")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

    def test_doctor_endpoint(self):
        _status, payload, _headers = self.call("/api/doctor")
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["data"]["ok"])
        checks = payload["data"]["checks"]
        self.assertTrue(any(c["level"] == "fail" for c in checks))
        self.assertTrue(all("level_label" in c for c in checks))


class TestLyricsCheck(WebTestCase):
    def test_clean_lyrics_ok(self):
        _status, payload, _headers = self.call(
            "/api/lyrics/check", "POST", {"lyrics": CLEAN_LYRICS}
        )
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["data"]["ok"])
        self.assertEqual(payload["data"]["max_chars"], 4096)

    def test_error_level(self):
        _status, payload, _headers = self.call(
            "/api/lyrics/check", "POST", {"lyrics": ERROR_LYRICS}
        )
        levels = {i["level"] for i in payload["data"]["issues"]}
        self.assertIn("error", levels)

    def test_warning_level(self):
        _status, payload, _headers = self.call(
            "/api/lyrics/check", "POST", {"lyrics": WARNING_LYRICS}
        )
        levels = {i["level"] for i in payload["data"]["issues"]}
        self.assertIn("warn", levels)

    def test_sections_have_display_label(self):
        _status, payload, _headers = self.call(
            "/api/lyrics/check", "POST", {"lyrics": CLEAN_LYRICS}
        )
        labels = [s["label"] for s in payload["data"]["sections"]]
        self.assertIn("主歌", labels)
        self.assertIn("副歌", labels)


class TestGenerate(WebTestCase):
    def test_generate_then_done(self):
        status, payload, _headers = self.call(
            "/api/generate", "POST", {"prompt": "温暖民谣", "lyrics": CLEAN_LYRICS, "duration": 120}
        )
        self.assertEqual(status, 202)
        self.assertTrue(payload["ok"])
        task_id = payload["data"]["task_id"]
        self.assertTrue(task_id)

        data = self.wait_done(task_id)
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["language"], "zh")
        self.assertIn("中文", data["language_label"])

        files = data["result"]["files"]
        self.assertEqual(len(files), 1)
        saved = self.songs_dir / files[0]["name"]
        self.assertTrue(saved.is_file(), f"文件没落盘：{saved}")
        self.assertTrue(saved.with_suffix(".json").is_file(), "复现记录没写")
        # 结果里带元数据
        self.assertEqual(data["result"]["bpm"], 77)
        self.assertEqual(data["result"]["key"], "D major")
        # 复现命令原文（完成页的折叠区要显示它，供会命令行的读者复制）
        self.assertIn("sfs make", data["result"]["reproduce"])
        self.assertEqual(data["result"]["summary"]["language"], "zh")

    def test_busy_returns_409(self):
        blocker = web.store.try_acquire_generation()
        self.assertTrue(blocker)
        try:
            status, payload, _headers = self.call(
                "/api/generate", "POST", {"prompt": "x", "lyrics": CLEAN_LYRICS, "duration": 120}
            )
        finally:
            web.store.release_generation()
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "busy")

    def test_concurrent_second_request_is_busy(self):
        # 让假客户端的 wait 停住一会儿，制造「正在生成」的窗口。
        class SlowClient(_FakeClient):
            def wait(self, task_id, timeout=1800.0, interval=5.0, on_progress=None):
                time.sleep(0.6)
                return super().wait(task_id, timeout, interval, on_progress)

        web._client_factory = lambda base, timeout: SlowClient(base, timeout)
        status1, payload1, _h = self.call(
            "/api/generate", "POST", {"prompt": "x", "lyrics": CLEAN_LYRICS, "duration": 120}
        )
        self.assertEqual(status1, 202)
        status2, payload2, _h = self.call(
            "/api/generate", "POST", {"prompt": "y", "lyrics": CLEAN_LYRICS, "duration": 120}
        )
        self.assertEqual(status2, 409)
        self.assertEqual(payload2["error"]["code"], "busy")
        self.wait_done(payload1["data"]["task_id"])

    def test_missing_prompt_or_lyrics(self):
        status, payload, _headers = self.call("/api/generate", "POST", {"prompt": "", "lyrics": ""})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "bad_request")

    def test_bad_duration(self):
        status, payload, _headers = self.call(
            "/api/generate", "POST", {"prompt": "x", "lyrics": CLEAN_LYRICS, "duration": 9999}
        )
        self.assertEqual(status, 422)
        self.assertEqual(payload["error"]["code"], "bad_duration")

    def test_lyrics_error_blocks_until_force(self):
        status, payload, _headers = self.call(
            "/api/generate", "POST", {"prompt": "x", "lyrics": ERROR_LYRICS, "duration": 120}
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "lyrics_error")
        self.assertIn("issues", payload["error"]["data"])

        status2, payload2, _headers = self.call(
            "/api/generate", "POST",
            {"prompt": "x", "lyrics": ERROR_LYRICS, "duration": 120, "force": True},
        )
        self.assertEqual(status2, 202)
        self.wait_done(payload2["data"]["task_id"])

    def test_lyrics_warning_blocks_until_yes(self):
        status, payload, _headers = self.call(
            "/api/generate", "POST", {"prompt": "x", "lyrics": WARNING_LYRICS, "duration": 120}
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "lyrics_warning")

        status2, payload2, _headers = self.call(
            "/api/generate", "POST",
            {"prompt": "x", "lyrics": WARNING_LYRICS, "duration": 120, "yes": True},
        )
        self.assertEqual(status2, 202)
        self.wait_done(payload2["data"]["task_id"])

    def test_service_unreachable_mapping(self):
        self.set_client(fail="unreachable")
        _status, payload, _headers = self.call(
            "/api/generate", "POST", {"prompt": "x", "lyrics": CLEAN_LYRICS, "duration": 120}
        )
        data = self.wait_done(payload["data"]["task_id"])
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["error"]["code"], "service_unreachable")
        self.assertNotIn("Traceback", json.dumps(data))

    def test_generate_failed_mapping(self):
        self.set_client(fail="generate")
        _status, payload, _headers = self.call(
            "/api/generate", "POST", {"prompt": "x", "lyrics": CLEAN_LYRICS, "duration": 120}
        )
        data = self.wait_done(payload["data"]["task_id"])
        self.assertEqual(data["status"], "failed")
        self.assertEqual(data["error"]["code"], "generate_failed")

    def test_language_english_when_mostly_non_cjk(self):
        english = "[Verse 1]\n" + "\n".join(["a line of english words"] * 6) + "\n[Chorus]\nsing it again my friend"
        _status, payload, _headers = self.call(
            "/api/generate", "POST", {"prompt": "x", "lyrics": english, "duration": 120, "force": True}
        )
        data = self.wait_done(payload["data"]["task_id"])
        self.assertEqual(data["language"], "en")
        self.assertIn("英文", data["language_label"])

    def test_out_prefix_in_filename(self):
        _status, payload, _headers = self.call(
            "/api/generate", "POST",
            {"prompt": "x", "lyrics": CLEAN_LYRICS, "duration": 120, "out_prefix": "给妈妈"},
        )
        self.assertTrue(payload["data"]["out_name"].startswith("给妈妈-song-"))
        self.wait_done(payload["data"]["task_id"])

    def test_summary_uses_friendly_keys(self):
        # 完成页折叠区读的是 summary.tempo / summary.steps 等友好键名，
        # 键名一旦写成契约原始名（如 bpm），界面上那一行会永远显示「模型自定」。
        _status, payload, _headers = self.call(
            "/api/generate", "POST",
            {"prompt": "x", "lyrics": CLEAN_LYRICS, "duration": 120, "tempo": 99, "steps": 12},
        )
        data = self.wait_done(payload["data"]["task_id"])
        summary = data["result"]["summary"]
        self.assertEqual(summary["tempo"], 99)
        self.assertEqual(summary["steps"], 12)
        self.assertNotIn("bpm", summary)

    def test_two_runs_same_minute_do_not_overwrite(self):
        # 「再来一版」会在同一分钟内连出多首，绝不能静默覆盖前一首。
        names = []
        for _ in range(2):
            _status, payload, _headers = self.call(
                "/api/generate", "POST",
                {"prompt": "x", "lyrics": CLEAN_LYRICS, "duration": 120, "out_prefix": "给爸爸"},
            )
            data = self.wait_done(payload["data"]["task_id"])
            names.append(data["result"]["files"][0]["name"])
        self.assertEqual(len(set(names)), 2, names)
        for name in names:
            self.assertTrue((self.songs_dir / name).is_file(), name)


class TestMediaAndPaths(WebTestCase):
    def _generate_one(self):
        _status, payload, _headers = self.call(
            "/api/generate", "POST", {"prompt": "x", "lyrics": CLEAN_LYRICS, "duration": 120}
        )
        data = self.wait_done(payload["data"]["task_id"])
        return data["result"]["files"][0]

    def test_media_serves_audio(self):
        file_info = self._generate_one()
        quoted = urllib.parse.quote(file_info["name"])
        status, _payload, headers = self.call("/media/" + quoted)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "audio/mpeg")

    def test_media_range_request(self):
        file_info = self._generate_one()
        quoted = urllib.parse.quote(file_info["name"])
        status, _payload, headers = self.call("/media/" + quoted, headers={"Range": "bytes=0-3"})
        self.assertEqual(status, 206)
        self.assertTrue(headers.get("Content-Range", "").startswith("bytes 0-3/"))

    def test_download_has_content_disposition(self):
        file_info = self._generate_one()
        quoted = urllib.parse.quote(file_info["name"])
        status, _payload, headers = self.call("/download/" + quoted)
        self.assertEqual(status, 200)
        disposition = headers.get("Content-Disposition", "")
        self.assertIn("attachment", disposition)
        self.assertIn("filename*=UTF-8''", disposition)

    def test_path_traversal_blocked(self):
        status, payload, _headers = self.call("/media/..%2f..%2fetc%2fpasswd")
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "forbidden")

    def test_missing_file_404(self):
        status, payload, _headers = self.call("/media/" + urllib.parse.quote("不存在.mp3"))
        self.assertEqual(status, 404)


class TestSongsAndCurrent(WebTestCase):
    def test_songs_empty_then_listed(self):
        _status, payload, _headers = self.call("/api/songs")
        self.assertEqual(payload["data"]["songs"], [])

        _status, request_payload, _headers = self.call(
            "/api/generate", "POST", {"prompt": "温暖民谣", "lyrics": CLEAN_LYRICS, "duration": 120}
        )
        self.wait_done(request_payload["data"]["task_id"])

        _status, payload2, _headers = self.call("/api/songs")
        songs = payload2["data"]["songs"]
        self.assertEqual(len(songs), 1)
        self.assertTrue(songs[0]["name"].endswith(".mp3"))
        self.assertEqual(songs[0]["bpm"], 77)
        self.assertIsNotNone(songs[0]["refill"])
        self.assertEqual(songs[0]["refill"]["prompt"], "温暖民谣")

    def test_current_is_null_when_idle(self):
        _status, payload, _headers = self.call("/api/tasks/current")
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["data"])

    def test_task_not_found(self):
        status, payload, _headers = self.call("/api/task/does-not-exist")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")


class TestHelpers(unittest.TestCase):
    """纯函数单测，不需要起服务器。"""

    def test_estimate_cold_is_larger(self):
        cold = web.estimate_seconds(120, 1, session_has_success=False)
        warm = web.estimate_seconds(120, 1, session_has_success=True)
        self.assertGreater(cold, warm)

    def test_estimate_scales_with_duration(self):
        short = web.estimate_seconds(60, 1, True)
        long = web.estimate_seconds(180, 1, True)
        self.assertLess(short, long)

    def test_estimate_minimum(self):
        self.assertGreaterEqual(web.estimate_seconds(10, 1, True), web.MIN_ESTIMATE)

    def test_decide_language_chinese(self):
        from song_for_someone.lyrics import analyze

        self.assertEqual(web.decide_language(analyze(CLEAN_LYRICS)), "zh")

    def test_decide_language_english(self):
        from song_for_someone.lyrics import analyze

        english = "hello world this is a song\nsing it again"
        self.assertEqual(web.decide_language(analyze(english)), "en")

    def test_decide_language_instrumental_is_zh(self):
        from song_for_someone.lyrics import analyze

        self.assertEqual(web.decide_language(analyze("[Instrumental]")), "zh")

    def test_sanitize_removes_illegal_chars(self):
        cleaned = sanitize_filename_part('a/b:c*?"<>|')
        self.assertNotIn("/", cleaned)
        self.assertNotIn(":", cleaned)
        self.assertNotIn("?", cleaned)

    def test_sanitize_truncates(self):
        self.assertLessEqual(len(sanitize_filename_part("x" * 100)), 32)

    def test_sanitize_empty_uses_fallback(self):
        self.assertEqual(sanitize_filename_part("   ", "fallback"), "fallback")

    def test_sanitize_keeps_chinese(self):
        self.assertIn("妈妈", sanitize_filename_part("给妈妈的歌"))

    def test_sanitize_windows_reserved(self):
        self.assertEqual(sanitize_filename_part("CON"), "_CON")

    def test_pick_server_falls_back_when_port_busy(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy_port = blocker.getsockname()[1]
        try:
            server, chosen = web.pick_server("127.0.0.1", busy_port, 5)
            self.assertIsNotNone(server)
            self.assertNotEqual(chosen, busy_port)
            server.server_close()
        finally:
            blocker.close()

    def test_host_is_loopback(self):
        self.assertEqual(web.HOST, "127.0.0.1")

    def test_no_cli_import(self):
        # 依赖方向红线：web.py 绝不 import cli。
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(web))
        relative = []
        absolute = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.level == 1:
                    relative.append(node.module)
            elif isinstance(node, ast.Import):
                absolute.extend(alias.name for alias in node.names)
        self.assertNotIn("cli", relative)
        self.assertNotIn("cli", absolute)


if __name__ == "__main__":
    unittest.main()
