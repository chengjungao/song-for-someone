# -*- coding: utf-8 -*-
"""上游引擎（ACE-Step API 服务）的探测与拉起。

双击 ``启动.bat`` 的人不该再开一个终端敲命令。这个模块负责把上游服务
一起带起来，并且**只认便携包自带的那套 Python**。

为什么不走官方的 ``start_api_server.bat``：那个脚本判的是
``python_embedded``（多一个 n），而便携包实际目录叫 ``python_embeded``
（少一个 n，上游自己的拼写前后不一致）。判断永远不成立，于是它掉进 uv 分支
去用 ``.venv`` —— 而便携包里的 ``.venv`` 常常是个空壳，连 torch 都没有。
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .client import DEFAULT_BASE_URL, AceStepClient
# 便携包怎么找、哪套 Python 能用，全都由 doctor 说了算 —— 起服务和自检必须
# 用同一个判据，否则就会出现「引擎自己找得到、自检说没有」这种怪事。
from .doctor import (
    EMBEDDED_DIR_ALIASES,
    find_package_root,
    has_torch,
    remembered_root,
    remember_root,
    validate_package_root,
)
from .doctor import find_embedded_python as _find_embedded_python

# ---------------------------------------------------------------- 常量

ENGINE_MODULE = "acestep.api_server"
DEFAULT_LOG_NAME = "_api_run.log"
TRITON_CACHE_DIRNAME = "_triton_cache"
# 让上游在**启动时**就把模型载入显存，而不是拖到第一次出歌。
#
# 上游默认是懒加载（`ACESTEP_NO_INIT` 默认 true，见上游
# `acestep/api/startup_model_init.py`）。懒加载下引擎十几秒就报「就绪」，
# 但真正的模型载入被推到第一次出歌 —— 实测那一次要等 72 秒（10 秒纯器乐）
# 到 172 秒（90 秒人声），而这段等待发生在界面上，用户只会以为卡住了。
# 改成预加载，把等待挪到这一步：这里有控制台进度，用户知道在等什么。
ENGINE_PRELOAD_ENV = {"ACESTEP_NO_INIT": "false"}

# 就绪探测：这是短请求，别用生成任务的超时。
HEALTH_TIMEOUT = 2.0

# 冷启动要等 planner（5Hz LM）初始化，实测 138 秒；首次还要下约 9.4GB 模型，
# 所以等待上限给得宽一些。子进程半路退出会立刻判失败，不会真白等这么久。
READY_TIMEOUT = 1800.0
POLL_INTERVAL = 2.0

# 状态常量
ALREADY_UP = "already-up"
ADOPTED = "adopted"
STARTED = "started"
NO_ROOT = "no-root"
NO_PYTHON = "no-python"
FAILED = "failed"


# ---------------------------------------------------------------- 探测


def is_engine_up(base_url: str = DEFAULT_BASE_URL, timeout: float = HEALTH_TIMEOUT) -> bool:
    """上游服务是不是已经在跑了。不抛异常。"""
    return AceStepClient(base_url, timeout=timeout).health()


def is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """端口上有没有人在监听。

    用来区分「没人起过」和「有人正在起」：上游初始化阶段端口已经开了，
    但 ``/health`` 还没通。这时候该等着，而不是再拉一个起来抢端口。
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_embedded_python(
    package_root: Path,
    require_torch: bool = True,
) -> Optional[Path]:
    """在便携包里找那套**真能干活**的 Python。

    这边默认就要带 torch —— 起服务时找到一套没 torch 的 Python 没有意义，
    只会把错误推后成更难懂的 ``ModuleNotFoundError``。要宽松档（先找到再说、
    把「没 torch」当成一个具体问题报出来）走 ``doctor.find_embedded_python``。
    """
    return _find_embedded_python(package_root, require_torch=require_torch)


def discover_root(hint: Optional[str] = None) -> Optional[Path]:
    """找一个**能真起服务**的便携包根目录。

    候选目录、Python 定位、torch 校验都在 ``doctor`` 里，这里只是以「必须带
    torch」的口径调一次。判据严一档是必要的：光看目录结构像，会把那个空的
    ``.venv`` 也放进来，最后报的错变成难懂的 ``ModuleNotFoundError``。
    """
    return find_package_root(hint, require_torch=True)


# ---------------------------------------------------------------- 拉起


@dataclass
class EngineHandle:
    """一个已经拉起来（还没确认就绪）的引擎进程。"""

    process: subprocess.Popen
    root: Path
    python_exe: Path
    log_path: Path
    command: List[str] = field(default_factory=list)

    @property
    def pid(self) -> Optional[int]:
        """子进程号。"""
        return self.process.pid

    def alive(self) -> bool:
        """进程还活着吗。"""
        return self.process.poll() is None

    def exit_code(self) -> Optional[int]:
        """已退出的话返回退出码，还活着返回 None。"""
        return self.process.poll()

    def command_line(self) -> str:
        """拼回一条可以直接粘到终端里的命令，方便排查。"""
        return subprocess.list2cmdline([str(part) for part in self.command])


def build_command(
    python_exe: Path,
    host: str,
    port: int,
    download_source: Optional[str] = "modelscope",
    lm_model: Optional[str] = None,
) -> List[str]:
    """拼出启动上游服务的命令行。

    不写死 LM 型号：上游会按显存自己挑（16GB 卡实测自动选 1.7B）。
    """
    command = [
        str(python_exe),
        "-m",
        ENGINE_MODULE,
        "--host",
        host,
        "--port",
        str(port),
    ]
    if download_source:
        command += ["--download-source", download_source]
    if lm_model:
        command += ["--lm-model-path", lm_model]
    return command


def start_engine(
    root: Path,
    python_exe: Path,
    host: str,
    port: int,
    log_path: Optional[Path] = None,
    download_source: Optional[str] = "modelscope",
    lm_model: Optional[str] = None,
) -> EngineHandle:
    """把上游服务拉起来，输出重定向到日志文件。

    引擎是调用方的子进程，**跟着调用方一起走**：``start.py`` 退出时会主动停它，
    释放显存。实测父进程退出后子进程也会被系统带走（日志停在
    ``Uvicorn running`` 之后，没有任何报错或 shutdown 记录），所以不要指望它
    自己常驻 —— 想让引擎长留着，就用便携包里的 ``启动引擎.bat`` 单独开着它，
    ``start.py`` 检测到已有引擎会直接复用。
    """
    if log_path is None:
        log_path = root / DEFAULT_LOG_NAME
    log_path = Path(log_path)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        (root / TRITON_CACHE_DIRNAME).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    child_env: Dict[str, str] = dict(os.environ)
    child_env["TRITON_CACHE_DIR"] = str(root / TRITON_CACHE_DIRNAME)
    child_env["PYTHONUNBUFFERED"] = "1"
    child_env.update(ENGINE_PRELOAD_ENV)
    # 别让外面那套 PYTHONPATH 漏进子进程，容易出现诡异的包冲突。
    child_env.pop("PYTHONPATH", None)

    command = build_command(python_exe, host, port, download_source, lm_model)

    creationflags = 0
    if os.name == "nt":
        # 让引擎成为新进程组，这样按 Ctrl+C 时它**不会**被一起打断，
        # 好让调用方有机会自己决定什么时候停它。
        # 注意：这**不**等于「脱离父进程独立存活」——实测父进程退出后它照样被带走。
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    log_file = open(log_path, "ab")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(root),
            env=child_env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    finally:
        # 子进程已经拿到句柄副本，父进程这份可以关了。
        log_file.close()

    return EngineHandle(
        process=process,
        root=root,
        python_exe=python_exe,
        log_path=log_path,
        command=command,
    )


def wait_until_ready(
    handle: Optional[EngineHandle] = None,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = READY_TIMEOUT,
    interval: float = POLL_INTERVAL,
    on_tick: Optional[Callable[[float], None]] = None,
) -> bool:
    """等上游服务就绪。

    ``handle`` 给 ``None`` 时表示进程不是我们起的（比如上一次启动还在初始化），
    这时只等，不管进程死活。

    子进程半路退出就立刻判失败并返回 —— 端口被占、模型缺失这类问题都会让
    进程秒退，没必要陪着等满超时。
    """
    started = time.monotonic()
    while True:
        if is_engine_up(base_url):
            return True

        if handle is not None and not handle.alive():
            return False

        elapsed = time.monotonic() - started
        if elapsed >= timeout:
            return False

        if on_tick is not None:
            try:
                on_tick(elapsed)
            except Exception:
                pass
        time.sleep(interval)


def tail_log(log_path: Path, lines: int = 20) -> str:
    """读日志末尾几行，用于失败时给用户一点线索。"""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read().splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


# ---------------------------------------------------------------- 一站式


@dataclass
class EngineOutcome:
    """``ensure_engine`` 的结果。"""

    status: str
    message: str
    root: Optional[Path] = None
    handle: Optional[EngineHandle] = None

    @property
    def ready(self) -> bool:
        """上游现在可用吗。"""
        return self.status in (ALREADY_UP, ADOPTED, STARTED)


def split_base_url(base_url: str) -> tuple:
    """从服务地址里取出 host 和 port。"""
    parsed = urllib.parse.urlsplit(base_url if "//" in base_url else "http://" + base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8001
    return host, port


def _make_ticker(emit: Callable[[str], None], every: float = 15.0) -> Callable[[float], None]:
    """做一个「每 N 秒报一次」的进度回调，免得刷屏。"""
    state = [0.0]

    def tick(elapsed: float) -> None:
        if elapsed - state[0] >= every:
            state[0] = elapsed
            emit(f"  等待引擎就绪… 已等 {int(elapsed)} 秒")

    return tick


def ensure_engine(
    base_url: str = DEFAULT_BASE_URL,
    root_hint: Optional[str] = None,
    download_source: Optional[str] = "modelscope",
    lm_model: Optional[str] = None,
    timeout: float = READY_TIMEOUT,
    log: Optional[Callable[[str], None]] = None,
) -> EngineOutcome:
    """确保上游引擎可用：已经在跑就用现成的，没跑就拉起来。

    :param log: 进度回调，收一行人类可读的中文。传 ``None`` 就静默。
    """
    emit = log if log is not None else (lambda _line: None)

    if is_engine_up(base_url):
        emit("音乐引擎已经在运行，直接用。")
        return EngineOutcome(status=ALREADY_UP, message="引擎已在运行")

    host, port = split_base_url(base_url)

    # 端口开着但 /health 还没通：上一次启动还在初始化。等它，别再拉一个抢端口。
    if is_port_open(host, port):
        emit(f"端口 {port} 上有引擎正在初始化（不是本窗口起的），等它…")
        if wait_until_ready(None, base_url, timeout=timeout, on_tick=_make_ticker(emit)):
            emit("音乐引擎已就绪。")
            return EngineOutcome(status=ADOPTED, message="接上了正在初始化的引擎")
        return EngineOutcome(
            status=FAILED,
            message=(
                f"等了 {int(timeout)} 秒，端口 {port} 上有程序在监听，但 /health 一直不通。\n"
                "可能那个程序不是 ACE-Step，只是占了同一个端口。\n"
                f"想看是谁占的：netstat -ano | findstr :{port}\n"
                "想换个端口：先关掉占用者，或用 --base-url 指定别的地址。"
            ),
        )

    root = discover_root(root_hint)
    if root is None:
        message = (
            "没找到 ACE-Step 便携包，音乐引擎起不来。\n"
            "解决办法：去下载官方便携包（2.4GB），解压到一个固定目录：\n"
            "  https://files.acemusic.ai/acemusic/win/ACE-Step-1.5.7z\n"
            "解压后重新双击本文件，也可以用 --engine-root 指定解压后的目录。"
        )
        emit("没找到 ACE-Step 便携包。")
        return EngineOutcome(status=NO_ROOT, message=message)

    python_exe = find_embedded_python(root, require_torch=True)
    if python_exe is None:
        message = (
            f"{root} 里没找到可用的 Python。\n"
            "这个目录里的 python_embeded 应该带 torch；如果只有 .venv，那是个空壳，\n"
            "上游的 start_api_server.bat 会误用它，用不了。\n"
            "解决办法：重新解压一次官方便携包。\n"
            "  https://files.acemusic.ai/acemusic/win/ACE-Step-1.5.7z"
        )
        emit(f"{root} 里没找到带 torch 的 Python。")
        return EngineOutcome(status=NO_PYTHON, message=message, root=root)

    emit(f"找到便携包：{root}")
    emit(f"用便携包自带的 Python：{python_exe}")
    emit("正在启动音乐引擎（首次运行要下约 10GB 模型，可能十几分钟）…")

    try:
        handle = start_engine(
            root=root,
            python_exe=python_exe,
            host=host,
            port=port,
            download_source=download_source,
            lm_model=lm_model,
        )
    except OSError as exc:
        message = f"音乐引擎没能启动：{exc}"
        emit(message)
        return EngineOutcome(status=FAILED, message=message, root=root)

    started_at = time.monotonic()
    ready = wait_until_ready(handle, base_url, timeout=timeout, on_tick=_make_ticker(emit))

    if ready:
        emit(f"音乐引擎已就绪（用时约 {int(time.monotonic() - started_at)} 秒）。")
        emit(f"引擎日志：{handle.log_path}")
        return EngineOutcome(status=STARTED, message="引擎已启动", root=root, handle=handle)

    exit_code = handle.exit_code()
    if exit_code is not None:
        message = (
            f"音乐引擎刚启动就退出了（退出码 {exit_code}）。\n"
            f"看日志找原因：{handle.log_path}\n"
            f"可以重现的命令：\n{handle.command_line()}\n"
            "常见原因：端口被别的程序占用、显存不够、模型文件缺失。"
        )
    else:
        message = (
            f"等了 {int(timeout)} 秒音乐引擎还没就绪。\n"
            f"它可能还在下模型，再看看日志：{handle.log_path}\n"
            "首次运行要下约 10GB 模型，慢是正常的。"
        )

    tail = tail_log(handle.log_path)
    if tail:
        message += "\n日志最后几行：\n" + tail

    emit("音乐引擎没起来。")
    return EngineOutcome(status=FAILED, message=message, root=root, handle=handle)


def stop_engine(handle: EngineHandle, timeout: float = 10.0) -> bool:
    """停掉引擎进程，释放显存。

    ``start.py`` 在退出前会调它。别指望子进程自己会走 —— Windows 上的清理
    时机不明确，主动停掉才不留占着显存的残骸。
    """
    if not handle.alive():
        return True
    try:
        handle.process.terminate()
        handle.process.wait(timeout=timeout)
        return True
    except Exception:
        try:
            handle.process.kill()
            return True
        except Exception:
            return False


# ---------------------------------------------------------------- 控制器

# 一次「启动引擎」任务的状态
JOB_IDLE = "idle"
JOB_STARTING = "starting"
JOB_READY = "ready"
JOB_FAILED = "failed"

# 便携包位置是哪来的
SOURCE_MANUAL = "manual"          # 用户在界面里填的
SOURCE_REMEMBERED = "remembered"  # 界面上次填过、记下来的
SOURCE_AUTO = "auto"              # 程序自己找到的

# 服务状态
SERVICE_OFF = "off"
SERVICE_INITIALIZING = "initializing"
SERVICE_RUNNING = "running"

# 探测结果的缓存时长。界面在启动期间每秒问一次，不必每次都去扫盘。
LOCATE_TTL = 5.0
MAX_JOB_LINES = 200


def _same_path(left: Optional[Path], right: Optional[Path]) -> bool:
    """两个路径是不是同一个地方（大小写不敏感，Windows 上必须如此）。"""
    if left is None or right is None:
        return False
    try:
        return str(Path(left).resolve()).lower() == str(Path(right).resolve()).lower()
    except OSError:
        return str(left).lower() == str(right).lower()


@dataclass
class EngineJob:
    """一次「启动引擎」的进度。界面靠它显示状态、耗时和日志。"""

    status: str = JOB_IDLE
    lines: List[str] = field(default_factory=list)
    message: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0

    def elapsed(self) -> float:
        """这次启动已经花了多久（秒）。"""
        if self.finished_at:
            return max(0.0, self.finished_at - self.started_at)
        if self.status == JOB_STARTING:
            return max(0.0, time.time() - self.started_at)
        return 0.0

    def to_public(self) -> dict:
        return {
            "status": self.status,
            "lines": list(self.lines),
            "message": self.message,
            "elapsed": round(self.elapsed(), 1),
        }


class EngineController:
    """引擎的「长期状态 ＋ 随时启停」，给网页界面用。

    和 ``ensure_engine`` 的分工：那个是「一次把引擎弄好」的函数，用完就走；
    这个对象要一直活着，因为界面开着的时候得能反复问状态、随时点起停。

    两条它替界面守住的原则：

    * **只停自己起的引擎**。复用别人起的（启动窗口、便携包里的启动引擎.bat）
      绝不能被顺手关掉 —— 那是别人的东西。
    * **别人的路径不写进配置**。手填的位置先校验再记住，免得一个拼错的路径
      把下次启动也拦住。
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        root_hint: Optional[str] = None,
    ) -> None:
        self.base_url = base_url
        self._root_hint: Optional[str] = root_hint
        self._handle: Optional[EngineHandle] = None
        self._lock = threading.Lock()
        self._job = EngineJob()
        self._locate_cache: Optional[tuple] = None

    # ------------------------------------------------------------ 句柄

    def adopt(self, handle: Optional[EngineHandle]) -> None:
        """接手一个外部起的引擎句柄（``start.py`` 用它把句柄交给界面）。

        不接手的话，界面里的「停止出歌程序」就没有东西可停 —— 那个进程是
        启动脚本起的，控制器手里只有一个 ``None``。
        """
        with self._lock:
            self._handle = handle

    def own_handle(self) -> Optional[EngineHandle]:
        """现在手上有没有我们负责停的引擎。"""
        with self._lock:
            return self._handle

    def set_base_url(self, base_url: str) -> None:
        """换上游地址（``web.main`` 解析完参数后调）。"""
        self.base_url = base_url

    # ------------------------------------------------------------ 探测

    @property
    def root_hint(self) -> Optional[str]:
        """用户手填的便携包位置（没填过就是 ``None``）。"""
        return self._root_hint

    def locate(self, refresh: bool = False) -> dict:
        """便携包在哪、里面那套 Python 能不能用。

        结果缓存几秒：界面在启动期间每秒问一次状态，不必每次都重新扫盘。
        """
        now = time.monotonic()
        if not refresh:
            with self._lock:
                cached = self._locate_cache
            if cached is not None and now - cached[0] < LOCATE_TTL:
                return dict(cached[1])

        info = self._locate(self._root_hint)
        with self._lock:
            self._locate_cache = (now, info)
        return dict(info)

    def _locate(self, hint: Optional[str]) -> dict:
        info: Dict[str, object] = {
            "root": None,
            "source": None,
            "python_exe": None,
            "has_torch": False,
            "usable": False,
            "manual_reason": "",
            "note": "",
        }

        # 用户手填的优先，但校验不过就继续往下找，只是把原因留下来告诉用户。
        # （不能因为他填错一次就整个不给自动找了。）
        if hint:
            manual_root, reason = validate_package_root(hint)
            info["manual_reason"] = reason
            if manual_root is not None:
                return self._describe(info, manual_root, SOURCE_MANUAL)

        found = find_package_root(None, require_torch=True)
        if found is not None:
            source = (
                SOURCE_REMEMBERED
                if _same_path(found, remembered_root())
                else SOURCE_AUTO
            )
            filled = self._describe(info, found, source)
            if hint and info["manual_reason"]:
                filled["note"] = (
                    f"你填的那个位置用不了，现在用的是自动找到的：{found}\n"
                    f"{info['manual_reason']}"
                )
            return filled

        if hint:
            info["note"] = str(info["manual_reason"])
            return info

        info["note"] = (
            "没找到 ACE-Step 便携包。已经下载解压的话，把那个文件夹的完整路径填到\n"
            "上面，点「启动出歌程序」，程序会先检查再启动。还没下载的话见 README 的\n"
            "「快速开始」：官方便携包 2.4GB（模型第一次出歌时自动下），"
            "或者用已经带模型的打包版。"
        )
        return info

    @staticmethod
    def _describe(info: dict, root: Path, source: str) -> dict:
        """把「找到了便携包」这件事补全成一个给界面看的 dict。"""
        python_exe = find_embedded_python(root, require_torch=False)
        info.update({
            "root": str(root),
            "source": source,
            "python_exe": str(python_exe) if python_exe is not None else None,
            "has_torch": python_exe is not None and has_torch(python_exe),
            "usable": python_exe is not None and has_torch(python_exe),
            "note": "",
        })
        return info

    def service_state(self) -> dict:
        """上游服务现在什么样：没起 / 正在初始化 / 已就绪。"""
        host, port = split_base_url(self.base_url)
        if not is_port_open(host, port, timeout=0.5):
            return {"state": SERVICE_OFF, "detail": "没连上出歌程序"}
        if is_engine_up(self.base_url, timeout=HEALTH_TIMEOUT):
            return {"state": SERVICE_RUNNING, "detail": "已连上出歌程序"}
        return {"state": SERVICE_INITIALIZING, "detail": "出歌程序可能还在初始化"}

    def snapshot(self, refresh: bool = False) -> dict:
        """界面需要的全部信息，一次给足（它每秒都会拿一次）。"""
        with self._lock:
            job = self._job
            handle = self._handle
        service = self.service_state()
        can_stop = handle is not None and handle.alive()

        return {
            "base_url": self.base_url,
            "service": service["state"],
            "service_detail": service["detail"],
            "starting": job.status == JOB_STARTING,
            "can_stop": can_stop,
            "stop_hint": self._stop_hint(can_stop, service["state"]),
            "job": job.to_public(),
            "package": self.locate(refresh=refresh),
        }

    @staticmethod
    def _stop_hint(can_stop: bool, service_state: str) -> str:
        """「停止」按钮按不了时说清为什么。"""
        if can_stop:
            return ""
        if service_state == SERVICE_OFF:
            return "出歌程序没在运行。"
        return (
            "这个出歌程序不是从这个界面起的（可能是启动窗口，或者便携包里的"
            "「启动引擎.bat」），所以这里停不掉它 —— 关掉那个窗口就停了。"
        )

    # ------------------------------------------------------------ 启停

    def start(
        self,
        root: Optional[str] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """把引擎拉起来。**立刻返回**，真正的工作在后台线程里做。

        界面拿到返回值就开始轮询 ``snapshot()`` 看进度，请求不会被挂住 ——
        冷启动要等一两分钟，挂住的请求会让人以为页面死了。
        """
        if root:
            resolved, reason = validate_package_root(root)
            if resolved is None:
                with self._lock:
                    # 只留在内存里，让界面能把「你填的这条为什么不行」原样报出来；
                    # **不写进配置文件** —— 一个拼错的路径不该把下次启动也拦住。
                    self._root_hint = str(root)
                    self._locate_cache = None
                    self._job.status = JOB_FAILED
                    self._job.started_at = time.time()
                    self._job.finished_at = time.time()
                    self._job.message = reason
                    self._job.lines = reason.splitlines()[-MAX_JOB_LINES:]
                return {"ok": False, "reason": "bad-root", "message": reason}
            resolved_text = str(resolved)
        else:
            resolved_text = None

        with self._lock:
            if self._job.status == JOB_STARTING:
                return {
                    "ok": False,
                    "reason": "busy",
                    "message": "已经在启动中了，这就不用重复点了。",
                }
            if resolved_text:
                self._root_hint = resolved_text
            hint = self._root_hint
            self._job = EngineJob(status=JOB_STARTING, started_at=time.time())
            self._locate_cache = None

        if resolved_text:
            # 写文件放到锁外：它慢，且失败了也不该影响这次启动
            remember_root(resolved_text)

        thread = threading.Thread(
            target=self._run_start,
            args=(hint, log),
            name="sfs-engine-start",
            daemon=True,
        )
        thread.start()
        return {"ok": True, "message": "正在启动出歌程序…"}

    def _run_start(self, hint: Optional[str], log: Optional[Callable[[str], None]]) -> None:
        """后台线程：老老实实等引擎起来，把每一行进度记进 job。"""

        def emit(line: str) -> None:
            with self._lock:
                self._job.lines.append(line)
                del self._job.lines[:-MAX_JOB_LINES]
            if log is not None:
                try:
                    log(line)
                except Exception:  # noqa: BLE001 —— 日志回调出错不该毁掉启动
                    pass

        try:
            outcome = ensure_engine(base_url=self.base_url, root_hint=hint, log=emit)
        except Exception as exc:  # noqa: BLE001 —— 后台线程里绝不能把异常漏出去
            outcome = EngineOutcome(
                status=FAILED, message=f"启动引擎时出错：{type(exc).__name__}: {exc}"
            )

        with self._lock:
            self._job.finished_at = time.time()
            if outcome.handle is not None:
                self._handle = outcome.handle
            if outcome.ready:
                self._job.status = JOB_READY
                self._job.message = outcome.message
            else:
                self._job.status = JOB_FAILED
                self._job.message = outcome.message
                self._job.lines.extend(outcome.message.splitlines())
                del self._job.lines[:-MAX_JOB_LINES]
            self._locate_cache = None

    def stop(self) -> dict:
        """停掉**我们自己起的**那个引擎，把显存还回去。

        别人起的引擎这里停不掉，也不该停 —— 返回 ``not-ours`` 让界面去解释。
        """
        with self._lock:
            handle = self._handle
        if handle is None:
            return {
                "ok": False,
                "reason": "not-ours",
                "message": (
                    "这个出歌程序不是从这个界面起的，这里停不了。\n"
                    "要停就关掉起它的那个窗口；或者用任务管理器结束那个 python 进程。"
                ),
            }

        ok = stop_engine(handle)
        message = (
            "出歌程序已停止，显存已释放。"
            if ok
            else "没能正常停掉。可以在任务管理器里结束那个 python 进程。"
        )
        with self._lock:
            self._handle = None
            if self._job.status == JOB_READY:
                self._job.status = JOB_IDLE
            self._job.message = message
            self._job.lines.append(message)
            del self._job.lines[:-MAX_JOB_LINES]
            self._locate_cache = None
        return {"ok": ok, "message": message}
