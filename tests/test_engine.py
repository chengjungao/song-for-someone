# -*- coding: utf-8 -*-
"""引擎编排（engine.py）的测试。

全程不碰真实显卡、不依赖本机那套便携包：

* 用临时目录造假的便携包结构（含「有目录但没 torch」这种空壳）；
* 用一个只实现 ``/health`` 的本地假服务冒充上游；
* 用假进程对象冒充上游子进程，不真的起 ACE-Step。

跑法：

    python -m unittest discover -s tests -t . -v
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from song_for_someone import engine  # noqa: E402


# ---------------------------------------------------------------- 测试替身


class FakeUpstream:
    """冒充上游 ACE-Step 服务，只实现 ``/health``。"""

    def __init__(self, healthy=True):
        self.healthy = healthy
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if outer.healthy and self.path == "/health":
                    body = json.dumps({"status": "ok"}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()

            def log_message(self, *args):  # 别把测试输出弄脏
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        return False


class DeafListener:
    """只占着端口、不回答任何 HTTP 请求 —— 模拟「引擎正在初始化」。"""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.sock.close()
        return False


class FakeProcess:
    """冒充 ``subprocess.Popen`` 的返回值。"""

    def __init__(self, exit_code=None, pid=4242):
        self.pid = pid
        self._exit_code = exit_code
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._exit_code

    def terminate(self):
        self.terminated = True
        self._exit_code = 0

    def kill(self):
        self.killed = True
        self._exit_code = -9

    def wait(self, timeout=None):
        return self._exit_code or 0


def make_portable(root, dirname="python_embeded", with_torch=True, with_acestep=True):
    """在 ``root`` 下造一个假的便携包。"""
    root = Path(root)
    python_dir = root / dirname
    python_dir.mkdir(parents=True, exist_ok=True)
    (python_dir / "python.exe").write_bytes(b"fake")
    if with_torch:
        site = python_dir / "Lib" / "site-packages"
        site.mkdir(parents=True, exist_ok=True)
        (site / "torch").mkdir(exist_ok=True)
    if with_acestep:
        (root / "acestep").mkdir(parents=True, exist_ok=True)
    return root


# ---------------------------------------------------------------- 探测


class TestSplitBaseUrl(unittest.TestCase):
    """从服务地址里拆 host / port。"""

    def test_plain(self):
        self.assertEqual(engine.split_base_url("http://127.0.0.1:8001"), ("127.0.0.1", 8001))

    def test_custom_port(self):
        self.assertEqual(engine.split_base_url("http://localhost:9999"), ("localhost", 9999))

    def test_missing_scheme(self):
        self.assertEqual(engine.split_base_url("127.0.0.1:8123"), ("127.0.0.1", 8123))

    def test_trailing_slash(self):
        self.assertEqual(engine.split_base_url("http://127.0.0.1:8001/"), ("127.0.0.1", 8001))

    def test_default_port_when_absent(self):
        self.assertEqual(engine.split_base_url("http://127.0.0.1"), ("127.0.0.1", 8001))


class TestHasTorch(unittest.TestCase):
    """判断某套 Python 能不能干活。"""

    def test_true_when_torch_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            exe = root / "python_embeded" / "python.exe"
            self.assertTrue(engine.has_torch(exe))

    def test_false_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp, with_torch=False)
            exe = root / "python_embeded" / "python.exe"
            self.assertFalse(engine.has_torch(exe))

    def test_lowercase_lib_layout(self):
        """类 Unix 布局也认。"""
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "python_embeded" / "bin" / "python3"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"fake")
            site = Path(tmp) / "python_embeded" / "lib" / "site-packages" / "torch"
            site.mkdir(parents=True)
            self.assertTrue(engine.has_torch(exe))


class TestFindEmbeddedPython(unittest.TestCase):
    """找到那套真能用的 Python。"""

    def test_hits_short_spelling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            found = engine.find_embedded_python(root)
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "python.exe")

    def test_hits_long_spelling(self):
        """上游脚本里那个多一个 n 的拼写也认。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp, dirname="python_embedded")
            found = engine.find_embedded_python(root)
            self.assertIsNotNone(found)

    def test_rejects_empty_shell(self):
        """关键用例：目录在、python 在，但没 torch —— 就是那个 .venv 空壳的情形。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp, with_torch=False)
            self.assertIsNone(engine.find_embedded_python(root))

    def test_missing_entirely(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(engine.find_embedded_python(Path(tmp)))

    def test_only_venv_is_not_enough(self):
        """.venv 即使有 python.exe 也不算数（它不在我们认的目录名里）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            venv_python = root / ".venv" / "Scripts"
            venv_python.mkdir(parents=True)
            (venv_python / "python.exe").write_bytes(b"fake")
            (root / ".venv" / "Lib" / "site-packages" / "torch").mkdir(parents=True)
            self.assertIsNone(engine.find_embedded_python(root))


class TestDiscoverRoot(unittest.TestCase):
    """找一个能真起服务的便携包。"""

    def test_hint_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            self.assertEqual(engine.discover_root(str(root)), root.resolve())

    def test_hint_rejected_without_torch(self):
        """结构像但没有 torch —— 不能用，必须继续找下一个候选。

        候选目录只有 doctor 一个来源，堵掉它就等于堵掉全盘的常见落点，
        否则会扫到本机真实存在的便携包，测试就变成在断言开发机的环境了。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp, with_torch=False)
            with mock.patch(
                "song_for_someone.doctor.candidate_roots", return_value=[]
            ):
                self.assertIsNone(engine.discover_root(str(root)))

    def test_hint_rejected_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "song_for_someone.doctor.candidate_roots", return_value=[]
            ):
                self.assertIsNone(engine.discover_root(str(Path(tmp) / "nope")))

    def test_returns_none_when_nothing_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "totally-empty"
            empty.mkdir()
            with mock.patch(
                "song_for_someone.doctor.candidate_roots", return_value=[]
            ):
                self.assertIsNone(engine.discover_root(str(empty)))


class TestPortProbe(unittest.TestCase):
    """端口探测：区分「没人起」和「正在起」。"""

    def test_open_when_listening(self):
        with DeafListener() as listener:
            self.assertTrue(engine.is_port_open("127.0.0.1", listener.port, timeout=0.5))

    def test_closed_when_nobody(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.assertFalse(engine.is_port_open("127.0.0.1", port, timeout=0.5))


class TestIsEngineUp(unittest.TestCase):
    """健康探测。"""

    def test_true_for_healthy(self):
        with FakeUpstream() as up:
            self.assertTrue(engine.is_engine_up(up.base_url, timeout=2.0))

    def test_false_for_unhealthy(self):
        with FakeUpstream(healthy=False) as up:
            self.assertFalse(engine.is_engine_up(up.base_url, timeout=2.0))

    def test_false_when_nobody_listens(self):
        self.assertFalse(engine.is_engine_up("http://127.0.0.1:1", timeout=1.0))


# ---------------------------------------------------------------- 命令行与句柄


class TestBuildCommand(unittest.TestCase):
    """启动命令要能直接粘进终端。"""

    def test_default_sources(self):
        cmd = engine.build_command(Path("py.exe"), "127.0.0.1", 8001)
        self.assertEqual(cmd[:3], ["py.exe", "-m", "acestep.api_server"])
        self.assertIn("--host", cmd)
        self.assertIn("8001", cmd)
        self.assertIn("--download-source", cmd)
        self.assertIn("modelscope", cmd)

    def test_lm_model_optional(self):
        cmd = engine.build_command(Path("py.exe"), "127.0.0.1", 8001)
        self.assertNotIn("--lm-model-path", cmd)

        cmd = engine.build_command(
            Path("py.exe"), "127.0.0.1", 8001, lm_model="acestep-5Hz-lm-1.7B"
        )
        self.assertIn("--lm-model-path", cmd)
        self.assertIn("acestep-5Hz-lm-1.7B", cmd)

    def test_download_source_can_be_omitted(self):
        cmd = engine.build_command(Path("py.exe"), "127.0.0.1", 8001, download_source=None)
        self.assertNotIn("--download-source", cmd)


class TestEngineHandle(unittest.TestCase):
    """句柄的几个小接口。"""

    def _handle(self, process):
        return engine.EngineHandle(
            process=process,
            root=Path("D:/portable"),
            python_exe=Path("D:/portable/python_embeded/python.exe"),
            log_path=Path("D:/portable/_api_run.log"),
            command=["py.exe", "-m", "acestep.api_server", "--port", "8001"],
        )

    def test_alive(self):
        self.assertTrue(self._handle(FakeProcess()).alive())

    def test_exit_code_when_dead(self):
        handle = self._handle(FakeProcess(exit_code=3))
        self.assertFalse(handle.alive())
        self.assertEqual(handle.exit_code(), 3)

    def test_command_line_is_pasteable(self):
        line = self._handle(FakeProcess()).command_line()
        self.assertIn("acestep.api_server", line)
        self.assertIn("--port", line)

    def test_pid(self):
        self.assertEqual(self._handle(FakeProcess(pid=777)).pid, 777)


class TestTailLog(unittest.TestCase):
    """失败时读日志末尾。"""

    def test_reads_last_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "a.log"
            log.write_text("\n".join(f"line{i}" for i in range(50)), encoding="utf-8")
            tail = engine.tail_log(log, lines=5)
            self.assertEqual(len(tail.splitlines()), 5)
            self.assertIn("line49", tail)

    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(engine.tail_log(Path(tmp) / "nope.log"), "")


# ---------------------------------------------------------------- 等待就绪


class TestWaitUntilReady(unittest.TestCase):
    """等就绪的三种结局。"""

    def test_ready_immediately(self):
        with FakeUpstream() as up:
            handle = engine.EngineHandle(
                process=FakeProcess(),
                root=Path("."),
                python_exe=Path("py.exe"),
                log_path=Path("x.log"),
            )
            self.assertTrue(
                engine.wait_until_ready(handle, up.base_url, timeout=2.0, interval=0.05)
            )

    def test_fails_fast_when_process_died(self):
        """进程秒退时立刻返回，不陪着等满超时。"""
        handle = engine.EngineHandle(
            process=FakeProcess(exit_code=1),
            root=Path("."),
            python_exe=Path("py.exe"),
            log_path=Path("x.log"),
        )
        started = time.monotonic()
        self.assertFalse(
            engine.wait_until_ready(
                handle, "http://127.0.0.1:1", timeout=30.0, interval=0.05
            )
        )
        self.assertLess(time.monotonic() - started, 5.0)

    def test_times_out(self):
        with FakeUpstream(healthy=False) as up:
            handle = engine.EngineHandle(
                process=FakeProcess(),
                root=Path("."),
                python_exe=Path("py.exe"),
                log_path=Path("x.log"),
            )
            self.assertFalse(
                engine.wait_until_ready(handle, up.base_url, timeout=0.3, interval=0.05)
            )

    def test_handle_none_only_waits(self):
        """handle 为空表示进程不是我们起的，只管等，不看死活。"""
        with DeafListener() as listener:
            base = f"http://127.0.0.1:{listener.port}"
            self.assertFalse(
                engine.wait_until_ready(None, base, timeout=0.3, interval=0.05)
            )

    def test_on_tick_called(self):
        with FakeUpstream(healthy=False) as up:
            ticks = []
            handle = engine.EngineHandle(
                process=FakeProcess(),
                root=Path("."),
                python_exe=Path("py.exe"),
                log_path=Path("x.log"),
            )
            engine.wait_until_ready(
                handle, up.base_url, timeout=0.3, interval=0.05, on_tick=ticks.append
            )
            self.assertTrue(ticks)

    def test_on_tick_exception_does_not_break_waiting(self):
        """进度回调自己炸了，不能把等待带崩。"""
        with FakeUpstream() as up:
            handle = engine.EngineHandle(
                process=FakeProcess(),
                root=Path("."),
                python_exe=Path("py.exe"),
                log_path=Path("x.log"),
            )

            def boom(_elapsed):
                raise RuntimeError("tick 里出错了")

            self.assertTrue(
                engine.wait_until_ready(
                    handle, up.base_url, timeout=2.0, interval=0.05, on_tick=boom
                )
            )


# ---------------------------------------------------------------- 一站式


class TestEnsureEngine(unittest.TestCase):
    """ensure_engine 的五种结局。"""

    def test_already_up(self):
        with FakeUpstream() as up:
            lines = []
            outcome = engine.ensure_engine(base_url=up.base_url, log=lines.append)
            self.assertEqual(outcome.status, engine.ALREADY_UP)
            self.assertTrue(outcome.ready)
            self.assertIsNone(outcome.handle)
            self.assertTrue(any("已经在运行" in line for line in lines))

    def test_no_root(self):
        with FakeUpstream(healthy=False) as up:
            with mock.patch.object(engine, "discover_root", return_value=None):
                with mock.patch.object(engine, "is_port_open", return_value=False):
                    outcome = engine.ensure_engine(base_url=up.base_url)
            self.assertEqual(outcome.status, engine.NO_ROOT)
            self.assertFalse(outcome.ready)
            self.assertIn("便携包", outcome.message)

    def test_no_python_because_no_torch(self):
        """目录结构在，但里面那套 Python 是空壳。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp, with_torch=False)
            with FakeUpstream(healthy=False) as up:
                with mock.patch.object(engine, "discover_root", return_value=root):
                    with mock.patch.object(engine, "is_port_open", return_value=False):
                        outcome = engine.ensure_engine(base_url=up.base_url)
            self.assertEqual(outcome.status, engine.NO_PYTHON)
            self.assertIn("torch", outcome.message)
            self.assertIn(".venv", outcome.message)

    def test_adopted_when_port_busy_but_health_slow(self):
        """有人在起引擎：端口开着、/health 还没通 —— 该等，而不是再拉一个。"""
        with DeafListener() as listener:
            base = f"http://127.0.0.1:{listener.port}"
            lines = []
            with mock.patch.object(engine, "start_engine") as fake_start:
                outcome = engine.ensure_engine(
                    base_url=base, timeout=0.3, log=lines.append
                )
            self.assertEqual(outcome.status, engine.FAILED)
            fake_start.assert_not_called()
            self.assertIn(str(listener.port), outcome.message)

    def test_adopted_success(self):
        """端口占着、随后 /health 通了 —— 判定为接上既有实例。"""
        with DeafListener() as listener:
            base = f"http://127.0.0.1:{listener.port}"
            with mock.patch.object(engine, "is_port_open", return_value=True):
                with mock.patch.object(engine, "wait_until_ready", return_value=True):
                    outcome = engine.ensure_engine(base_url=base, timeout=0.3)
            self.assertEqual(outcome.status, engine.ADOPTED)
            self.assertTrue(outcome.ready)

    def test_started(self):
        """没人在跑：拉起子进程并等到就绪。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            lines = []
            with mock.patch.object(engine, "discover_root", return_value=root):
                with mock.patch.object(engine, "is_port_open", return_value=False):
                    with mock.patch.object(
                        engine, "is_engine_up", side_effect=[False, True]
                    ):
                        with mock.patch.object(
                            engine, "subprocess"
                        ) as fake_subprocess:
                            fake_subprocess.Popen.return_value = FakeProcess()
                            fake_subprocess.STDOUT = -2
                            fake_subprocess.list2cmdline = lambda parts: " ".join(parts)
                            fake_subprocess.CREATE_NEW_PROCESS_GROUP = 0
                            outcome = engine.ensure_engine(
                                base_url="http://127.0.0.1:8001", log=lines.append
                            )
            self.assertEqual(outcome.status, engine.STARTED)
            self.assertTrue(outcome.ready)
            self.assertIsNotNone(outcome.handle)
            self.assertTrue(any("正在启动音乐引擎" in line for line in lines))

    def test_failed_when_process_exits_early(self):
        """子进程刚起就退出：要给出退出码和日志线索。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            with mock.patch.object(engine, "discover_root", return_value=root):
                with mock.patch.object(engine, "is_port_open", return_value=False):
                    with mock.patch.object(engine, "is_engine_up", return_value=False):
                        with mock.patch.object(engine, "subprocess") as fake_subprocess:
                            fake_subprocess.Popen.return_value = FakeProcess(exit_code=1)
                            fake_subprocess.STDOUT = -2
                            fake_subprocess.list2cmdline = lambda parts: " ".join(parts)
                            fake_subprocess.CREATE_NEW_PROCESS_GROUP = 0
                            outcome = engine.ensure_engine(
                                base_url="http://127.0.0.1:8001", timeout=0.3
                            )
            self.assertEqual(outcome.status, engine.FAILED)
            self.assertIn("退出码 1", outcome.message)
            self.assertIsNotNone(outcome.handle)

    def test_failed_when_start_engine_raises(self):
        """连进程都拉不起来（比如 Python 路径没了）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            with mock.patch.object(engine, "discover_root", return_value=root):
                with mock.patch.object(engine, "is_port_open", return_value=False):
                    with mock.patch.object(engine, "is_engine_up", return_value=False):
                        with mock.patch.object(
                            engine, "start_engine", side_effect=OSError("没有那个文件")
                        ):
                            outcome = engine.ensure_engine(
                                base_url="http://127.0.0.1:8001", timeout=0.3
                            )
            self.assertEqual(outcome.status, engine.FAILED)
            self.assertIn("没能启动", outcome.message)


class TestStartEngine(unittest.TestCase):
    """真的调 Popen 的那部分（进程本身是打的桩）。"""

    def test_writes_log_and_sets_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            python_exe = root / "python_embeded" / "python.exe"
            captured = {}

            def fake_popen(command, **kwargs):
                captured["command"] = command
                captured["kwargs"] = kwargs
                return FakeProcess()

            with mock.patch.object(engine.subprocess, "Popen", side_effect=fake_popen):
                handle = engine.start_engine(
                    root=root,
                    python_exe=python_exe,
                    host="127.0.0.1",
                    port=8001,
                )

            self.assertEqual(handle.root, root)
            self.assertTrue(handle.log_path.exists())
            self.assertEqual(captured["kwargs"]["cwd"], str(root))
            # triton 缓存必须指到包里，别落到用户目录
            self.assertEqual(
                captured["kwargs"]["env"]["TRITON_CACHE_DIR"],
                str(root / engine.TRITON_CACHE_DIRNAME),
            )
            self.assertEqual(captured["kwargs"]["env"]["PYTHONUNBUFFERED"], "1")
            # 预加载：别让第一首出歌去补模型载入的那一两分钟
            self.assertEqual(captured["kwargs"]["env"]["ACESTEP_NO_INIT"], "false")
            # 外部的 PYTHONPATH 不能漏进子进程
            self.assertNotIn("PYTHONPATH", captured["kwargs"]["env"])
            self.assertEqual(captured["kwargs"]["stdout"].name, str(handle.log_path))

    def test_creates_triton_cache_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            with mock.patch.object(engine.subprocess, "Popen", return_value=FakeProcess()):
                engine.start_engine(
                    root=root,
                    python_exe=root / "python_embeded" / "python.exe",
                    host="127.0.0.1",
                    port=8001,
                )
            self.assertTrue((root / engine.TRITON_CACHE_DIRNAME).is_dir())

    def test_custom_log_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            log = Path(tmp) / "sub" / "run.log"
            with mock.patch.object(engine.subprocess, "Popen", return_value=FakeProcess()):
                handle = engine.start_engine(
                    root=root,
                    python_exe=root / "python_embeded" / "python.exe",
                    host="127.0.0.1",
                    port=8001,
                    log_path=log,
                )
            self.assertEqual(handle.log_path, log)
            self.assertTrue(log.parent.is_dir())


class TestStopEngine(unittest.TestCase):
    """停引擎：界面停了要把显存还回去。"""

    def test_stops_running_process(self):
        process = FakeProcess()
        handle = engine.EngineHandle(
            process=process,
            root=Path("."),
            python_exe=Path("py.exe"),
            log_path=Path("x.log"),
        )
        self.assertTrue(engine.stop_engine(handle))
        self.assertTrue(process.terminated)

    def test_already_dead_is_fine(self):
        handle = engine.EngineHandle(
            process=FakeProcess(exit_code=0),
            root=Path("."),
            python_exe=Path("py.exe"),
            log_path=Path("x.log"),
        )
        self.assertTrue(engine.stop_engine(handle))

    def test_kills_a_real_process(self):
        """用真子进程验一次，别只信假进程。"""
        import subprocess

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        handle = engine.EngineHandle(
            process=proc,
            root=Path("."),
            python_exe=Path("py.exe"),
            log_path=Path("x.log"),
        )
        try:
            self.assertTrue(engine.stop_engine(handle, timeout=10.0))
            self.assertIsNotNone(proc.returncode, "真进程应该已经被停掉")
        finally:
            if proc.poll() is None:
                proc.kill()


class TestStartScriptHandoff(unittest.TestCase):
    """start.py 的引擎交接：只停自己起的那个。"""

    def setUp(self):
        import start as start_mod

        self.start = start_mod

    @staticmethod
    def _fake_outcome(status, handle=None):
        return engine.EngineOutcome(status=status, message="", handle=handle)

    def test_no_engine_flag_returns_none(self):
        self.assertIsNone(
            self.start._prepare_engine("http://127.0.0.1:8001", None, False)
        )

    def test_started_hands_back_handle(self):
        handle = object()
        with mock.patch.object(
            engine, "ensure_engine", return_value=self._fake_outcome(engine.STARTED, handle)
        ):
            got = self.start._prepare_engine("http://127.0.0.1:8001", None, True)
        self.assertIs(got, handle)

    def test_already_up_is_not_ours(self):
        """引擎本来就是别人起的，绝不能顺手关掉。"""
        with mock.patch.object(
            engine, "ensure_engine", return_value=self._fake_outcome(engine.ALREADY_UP, object())
        ):
            self.assertIsNone(
                self.start._prepare_engine("http://127.0.0.1:8001", None, True)
            )

    def test_adopted_is_not_ours(self):
        with mock.patch.object(
            engine, "ensure_engine", return_value=self._fake_outcome(engine.ADOPTED, object())
        ):
            self.assertIsNone(
                self.start._prepare_engine("http://127.0.0.1:8001", None, True)
            )

    def test_failed_is_not_ours(self):
        with mock.patch.object(
            engine, "ensure_engine", return_value=self._fake_outcome(engine.FAILED)
        ):
            self.assertIsNone(
                self.start._prepare_engine("http://127.0.0.1:8001", None, True)
            )

    def test_stop_none_does_nothing(self):
        with mock.patch.object(engine, "stop_engine") as fake:
            self.start._stop_engine(None)
        fake.assert_not_called()

    def test_stop_forwards_handle(self):
        handle = object()
        with mock.patch.object(engine, "stop_engine", return_value=True) as fake:
            self.start._stop_engine(handle)
        fake.assert_called_once_with(handle)

    def test_main_stops_own_engine_after_ui_exits(self):
        """界面一停（正常退出 / Ctrl+C），自己起的引擎必须被停掉。"""
        handle = object()
        with mock.patch.object(sys, "argv", ["start.py"]):
            with mock.patch.object(self.start, "_prepare_engine", return_value=handle):
                with mock.patch.object(self.start, "_stop_engine") as fake_stop:
                    with mock.patch("song_for_someone.web.main", return_value=0):
                        rc = self.start.main()
        self.assertEqual(rc, 0)
        fake_stop.assert_called_once_with(handle)

    def test_main_stops_engine_even_when_ui_crashes(self):
        """界面启动就炸，引擎也不能留着占显存。"""
        handle = object()
        with mock.patch.object(sys, "argv", ["start.py"]):
            with mock.patch.object(self.start, "_prepare_engine", return_value=handle):
                with mock.patch.object(self.start, "_stop_engine") as fake_stop:
                    with mock.patch.object(self.start, "_pause"):
                        with mock.patch(
                            "song_for_someone.web.main",
                            side_effect=RuntimeError("界面炸了"),
                        ):
                            rc = self.start.main()
        self.assertEqual(rc, 1)
        fake_stop.assert_called_once_with(handle)

    def test_main_with_no_engine_flag_stops_nothing(self):
        with mock.patch.object(sys, "argv", ["start.py", "--no-engine"]):
            with mock.patch.object(self.start, "_stop_engine") as fake_stop:
                with mock.patch("song_for_someone.web.main", return_value=0):
                    self.start.main()
        fake_stop.assert_called_once_with(None)


class TestStatusConstants(unittest.TestCase):
    """状态常量别被手滑改掉 —— 别处按字面值判断。"""

    def test_values(self):
        self.assertEqual(engine.ALREADY_UP, "already-up")
        self.assertEqual(engine.ADOPTED, "adopted")
        self.assertEqual(engine.STARTED, "started")
        self.assertEqual(engine.NO_ROOT, "no-root")
        self.assertEqual(engine.NO_PYTHON, "no-python")
        self.assertEqual(engine.FAILED, "failed")

    def test_embedded_aliases_cover_both_spellings(self):
        self.assertIn("python_embeded", engine.EMBEDDED_DIR_ALIASES)
        self.assertIn("python_embedded", engine.EMBEDDED_DIR_ALIASES)

    def test_preload_env_asks_upstream_for_eager_loading(self):
        """上游默认懒加载；我们要的是启动时载入，别改了它。"""
        self.assertEqual(engine.ENGINE_PRELOAD_ENV, {"ACESTEP_NO_INIT": "false"})


# ---------------------------------------------------------------- 控制器


def make_handle(alive=True, pid=4242):
    """造一个假句柄。假进程，不真的起任何东西。"""
    return engine.EngineHandle(
        process=FakeProcess(exit_code=None if alive else 0, pid=pid),
        root=Path("D:/fake-portable"),
        python_exe=Path("D:/fake-portable/python_embeded/python.exe"),
        log_path=Path("D:/fake-portable/_api_run.log"),
    )


class ControllerCase(unittest.TestCase):
    """控制器的公共前提：探测、健康检查、启停全部换成假的，不碰真实环境。

    默认状态是「没服务、没便携包」，每个用例按需覆盖其中一项。
    """

    def setUp(self):
        self.ctl = engine.EngineController(base_url="http://127.0.0.1:65500")
        self._patch("is_port_open", return_value=False)
        self._patch("is_engine_up", return_value=False)
        self._patch("find_package_root", return_value=None)
        self._patch("remembered_root", return_value=None)

    def _patch(self, name, **kwargs):
        """换掉 engine 里的一个符号，返回那个假对象（带 assert_called_* 系列）。"""
        patcher = mock.patch.object(engine, name, **kwargs)
        fake = patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def new_controller(self, root_hint=None):
        """换一个带初始 root_hint 的控制器（走公开的构造参数，不碰私有字段）。"""
        return engine.EngineController(
            base_url="http://127.0.0.1:65500", root_hint=root_hint
        )

    def wait_job(self, timeout=5.0):
        """等后台那次启动结束。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.ctl.snapshot()["job"]["status"] != engine.JOB_STARTING:
                return
            time.sleep(0.02)
        self.fail("后台启动没有在预期时间内结束")


class TestControllerSnapshot(ControllerCase):
    """控制器给界面的那份状态。"""

    def test_off_when_service_down(self):
        snap = self.ctl.snapshot()
        self.assertEqual(snap["service"], "off")
        self.assertFalse(snap["starting"])
        self.assertFalse(snap["can_stop"])
        self.assertIn("没找到", snap["package"]["note"])
        self.assertEqual(snap["job"]["status"], engine.JOB_IDLE)

    def test_running(self):
        self._patch("is_port_open", return_value=True)
        self._patch("is_engine_up", return_value=True)
        snap = self.ctl.snapshot()
        self.assertEqual(snap["service"], "running")
        self.assertIn("已连上", snap["service_detail"])

    def test_initializing_is_its_own_state(self):
        """端口开着但 /health 不通 = 有人在起它。别把它当成「没在跑」。"""
        self._patch("is_port_open", return_value=True)
        snap = self.ctl.snapshot()
        self.assertEqual(snap["service"], "initializing")
        self.assertIn("初始化", snap["service_detail"])

    def test_can_stop_only_for_a_live_handle_we_own(self):
        self.assertFalse(self.ctl.snapshot()["can_stop"])
        self.ctl.adopt(make_handle(alive=False))
        self.assertFalse(self.ctl.snapshot()["can_stop"])
        self.ctl.adopt(make_handle(alive=True))
        self.assertTrue(self.ctl.snapshot()["can_stop"])

    def test_stop_hint_explains_idle_case(self):
        self.assertIn("没在运行", self.ctl.snapshot()["stop_hint"])

    def test_stop_hint_explains_not_ours_case(self):
        """别人起的引擎停不了 —— 界面得说清为什么，不能留个按了没反应的按钮。"""
        self._patch("is_port_open", return_value=True)
        self._patch("is_engine_up", return_value=True)
        hint = self.ctl.snapshot()["stop_hint"]
        self.assertIn("不是从这个界面起的", hint)

    def test_no_stop_hint_when_we_can_stop(self):
        self.ctl.adopt(make_handle(alive=True))
        self.assertEqual(self.ctl.snapshot()["stop_hint"], "")

    def test_job_public_shape(self):
        job = engine.EngineJob(status=engine.JOB_FAILED, message="坏了")
        job.started_at = time.time() - 3
        job.finished_at = time.time()
        public = job.to_public()
        self.assertEqual(public["status"], engine.JOB_FAILED)
        self.assertEqual(public["message"], "坏了")
        self.assertEqual(public["lines"], [])
        self.assertGreaterEqual(public["elapsed"], 2.5)


class TestControllerLocate(ControllerCase):
    """便携包在哪、哪套 Python 能用。"""

    def test_finds_package_automatically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            self._patch("find_package_root", return_value=root)
            pkg = self.ctl.locate()
        self.assertEqual(pkg["root"], str(root))
        self.assertEqual(pkg["source"], engine.SOURCE_AUTO)
        self.assertTrue(pkg["usable"])
        self.assertTrue(pkg["has_torch"])
        self.assertTrue(pkg["python_exe"].endswith("python.exe"))

    def test_labels_the_remembered_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            self._patch("find_package_root", return_value=root)
            self._patch("remembered_root", return_value=root)
            pkg = self.ctl.locate()
        self.assertEqual(pkg["source"], engine.SOURCE_REMEMBERED)

    def test_manual_path_wins_and_skips_the_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            manual = make_portable(Path(tmp) / "手动")
            auto = make_portable(Path(tmp) / "自动")
            self._patch("validate_package_root", return_value=(manual, ""))
            finder = self._patch("find_package_root", return_value=auto)
            pkg = self.new_controller(root_hint=str(manual)).locate(refresh=True)
        self.assertEqual(pkg["root"], str(manual))
        self.assertEqual(pkg["source"], engine.SOURCE_MANUAL)
        finder.assert_not_called()

    def test_manual_failure_still_falls_back_to_auto(self):
        """填错一次不该把自动查找也废掉 —— 但要老实说清你填的那个为什么不行。"""
        with tempfile.TemporaryDirectory() as tmp:
            auto = make_portable(Path(tmp) / "自动")
            self._patch("validate_package_root", return_value=(None, "没有这个文件夹：D:\\nope"))
            self._patch("find_package_root", return_value=auto)
            pkg = self.new_controller(root_hint="D:\\nope").locate(refresh=True)
        self.assertEqual(pkg["root"], str(auto))
        self.assertEqual(pkg["source"], engine.SOURCE_AUTO)
        self.assertIn("用不了", pkg["note"])
        self.assertIn("没有这个文件夹", pkg["note"])

    def test_manual_failure_alone_reports_the_reason(self):
        self._patch("validate_package_root", return_value=(None, "没有这个文件夹：D:\\nope"))
        pkg = self.new_controller(root_hint="D:\\nope").locate(refresh=True)
        self.assertIsNone(pkg["root"])
        self.assertFalse(pkg["usable"])
        self.assertIn("没有这个文件夹", pkg["note"])

    def test_unusable_package_is_not_claimed_usable(self):
        """目录在、但里面没 torch：要如实报 unusable，不能只因为找到了就当好用。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp, with_torch=False)
            self._patch("find_package_root", return_value=root)
            pkg = self.ctl.locate()
        self.assertFalse(pkg["usable"])
        self.assertFalse(pkg["has_torch"])

    def test_locate_caches_until_refresh(self):
        finder = self._patch("find_package_root", return_value=None)
        self.ctl.locate()
        self.ctl.locate()
        self.assertEqual(finder.call_count, 1)
        self.ctl.locate(refresh=True)
        self.assertEqual(finder.call_count, 2)


class TestControllerStart(ControllerCase):
    """启动：立刻返回，后台干活，进度写进 job。"""

    def test_bad_root_is_rejected_before_anything_starts(self):
        self._patch("validate_package_root", return_value=(None, "没有这个文件夹：D:\\nope"))
        remember = self._patch("remember_root")
        ensure = self._patch("ensure_engine")

        result = self.ctl.start("D:\\nope")

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "bad-root")
        self.assertIn("没有这个文件夹", result["message"])
        remember.assert_not_called()   # 错的路径不许写进配置
        ensure.assert_not_called()     # 也不许真去起进程
        self.assertEqual(self.ctl.snapshot()["job"]["status"], engine.JOB_FAILED)

    def test_bad_root_stays_queryable_so_the_ui_can_explain(self):
        self._patch("validate_package_root", return_value=(None, "这里不行"))
        self.ctl.start("D:\\nope")
        self.assertIn("这里不行", self.ctl.locate(refresh=True)["note"])

    def test_good_root_is_remembered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_portable(tmp)
            self._patch("validate_package_root", return_value=(root, ""))
            remember = self._patch("remember_root")
            self._patch("ensure_engine", return_value=engine.EngineOutcome(
                status=engine.STARTED, message="引擎已启动"))

            self.assertTrue(self.ctl.start(str(root))["ok"])
            self.wait_job()

        remember.assert_called_once_with(str(root))
        self.assertEqual(self.ctl.root_hint, str(root))

    def test_start_takes_ownership_of_the_new_handle(self):
        handle = make_handle()
        outcome = engine.EngineOutcome(
            status=engine.STARTED, message="引擎已启动", handle=handle
        )
        self._patch("ensure_engine", return_value=outcome)
        self.ctl.start()
        self.wait_job()

        self.assertIs(self.ctl.own_handle(), handle)
        self.assertTrue(self.ctl.snapshot()["can_stop"])

    def test_already_running_does_not_take_ownership(self):
        """复用别人起的引擎：job 是 ready，但「停止」不该亮 —— 那不是我起的。"""
        self._patch("ensure_engine", return_value=engine.EngineOutcome(
            status=engine.ALREADY_UP, message="引擎已在运行"))
        self.ctl.start()
        self.wait_job()

        self.assertEqual(self.ctl.snapshot()["job"]["status"], engine.JOB_READY)
        self.assertIsNone(self.ctl.own_handle())

    def test_failure_keeps_the_reason_in_message_and_lines(self):
        self._patch("ensure_engine", return_value=engine.EngineOutcome(
            status=engine.FAILED,
            message="音乐引擎刚启动就退出了（退出码 1）。\n看日志找原因：D:\\x.log"))
        self.ctl.start()
        self.wait_job()

        job = self.ctl.snapshot()["job"]
        self.assertEqual(job["status"], engine.JOB_FAILED)
        self.assertIn("退出码 1", job["message"])
        self.assertTrue(any("退出码 1" in line for line in job["lines"]))

    def test_second_start_while_starting_is_busy(self):
        released = threading.Event()

        def slow_ensure(**_kwargs):
            released.wait(5.0)
            return engine.EngineOutcome(status=engine.FAILED, message="算了")

        self._patch("ensure_engine", side_effect=slow_ensure)
        self.assertTrue(self.ctl.start()["ok"])
        again = self.ctl.start()

        self.assertFalse(again["ok"])
        self.assertEqual(again["reason"], "busy")

        released.set()
        self.wait_job()

    def test_log_callback_gets_the_same_lines_as_the_job(self):
        seen = []

        def fake_ensure(base_url=None, root_hint=None, log=None, **_kwargs):
            log("找到便携包：D:\\x")
            log("正在启动音乐引擎…")
            return engine.EngineOutcome(status=engine.STARTED, message="ok")

        self._patch("ensure_engine", side_effect=fake_ensure)
        self.ctl.start(log=seen.append)
        self.wait_job()

        self.assertEqual(seen, ["找到便携包：D:\\x", "正在启动音乐引擎…"])
        self.assertEqual(self.ctl.snapshot()["job"]["lines"], seen)

    def test_log_callback_error_does_not_break_startup(self):
        def boom(_line):
            raise RuntimeError("回调自己炸了")

        self._patch("ensure_engine", return_value=engine.EngineOutcome(
            status=engine.STARTED, message="ok"))
        self.ctl.start(log=boom)
        self.wait_job()

        self.assertEqual(self.ctl.snapshot()["job"]["status"], engine.JOB_READY)

    def test_exception_in_thread_is_caught(self):
        self._patch("ensure_engine", side_effect=RuntimeError("炸了"))
        self.ctl.start()
        self.wait_job()

        job = self.ctl.snapshot()["job"]
        self.assertEqual(job["status"], engine.JOB_FAILED)
        self.assertIn("RuntimeError", job["message"])

    def test_starting_flag_is_true_while_the_thread_is_working(self):
        """界面靠这个标志决定要不要接着轮询，所以它必须在启动期间是 true。"""
        released = threading.Event()

        def slow_ensure(**_kwargs):
            released.wait(5.0)
            return engine.EngineOutcome(status=engine.STARTED, message="ok")

        self._patch("ensure_engine", side_effect=slow_ensure)
        self.ctl.start()

        snapshot = self.ctl.snapshot()
        self.assertTrue(snapshot["starting"])
        self.assertEqual(snapshot["job"]["status"], engine.JOB_STARTING)

        released.set()
        self.wait_job()
        self.assertFalse(self.ctl.snapshot()["starting"])


class TestControllerStop(ControllerCase):
    """停止：只停自己起的那个。"""

    def test_without_handle_says_not_ours(self):
        result = self.ctl.stop()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "not-ours")
        self.assertIn("界面", result["message"])

    def test_stops_our_engine_and_lets_go_of_the_handle(self):
        handle = make_handle(alive=True)
        self.ctl.adopt(handle)
        killer = self._patch("stop_engine", return_value=True)

        result = self.ctl.stop()

        self.assertTrue(result["ok"])
        killer.assert_called_once_with(handle)
        self.assertIsNone(self.ctl.own_handle())
        self.assertFalse(self.ctl.snapshot()["can_stop"])

    def test_failure_to_stop_is_reported_not_swallowed(self):
        self.ctl.adopt(make_handle(alive=True))
        self._patch("stop_engine", return_value=False)

        result = self.ctl.stop()

        self.assertFalse(result["ok"])
        self.assertIn("任务管理器", result["message"])
        self.assertIn("任务管理器", self.ctl.snapshot()["job"]["message"])

    def test_stop_after_a_successful_start_resets_the_job(self):
        handle = make_handle()
        self._patch("ensure_engine", return_value=engine.EngineOutcome(
            status=engine.STARTED, message="引擎已启动", handle=handle))
        self.ctl.start()
        self.wait_job()
        self.assertEqual(self.ctl.snapshot()["job"]["status"], engine.JOB_READY)

        self._patch("stop_engine", return_value=True)
        self.ctl.stop()

        self.assertEqual(self.ctl.snapshot()["job"]["status"], engine.JOB_IDLE)
        self.assertIn("已停止", self.ctl.snapshot()["job"]["message"])

    def test_stop_never_calls_with_a_missing_handle(self):
        killer = self._patch("stop_engine", return_value=True)
        self.ctl.stop()
        killer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
