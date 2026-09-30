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
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .client import DEFAULT_BASE_URL, AceStepClient

# ---------------------------------------------------------------- 常量

# 便携包里自带 Python 的目录名。两种拼写都认：上游文档用短的那个，
# 上游脚本用长的那个，谁也说不准下一个版本会叫哪个。
EMBEDDED_DIR_ALIASES = ("python_embeded", "python_embedded")

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

# 便携包常见的落点。只做固定候选 ＋ 一层通配，不扫全盘。
EXTRA_ROOTS = (
    "ACE-Step-1.5-portable",
    "Works/ACE-Step-1.5-portable",
    "AI/ACE-Step-1.5-portable",
    "Downloads/ACE-Step-1.5-portable",
)

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


def has_torch(python_exe: Path) -> bool:
    """便携包里那套 Python 有没有 torch。

    只看 site-packages，不起子进程 —— 启动路径上不该多花几秒去 import torch。
    这一关就是用来挡 ``.venv`` 那种空壳的。

    两种目录布局都要认：Windows 便携包是 ``python_embeded\\python.exe`` ＋
    ``Lib\\site-packages``，类 Unix 是 ``python_embeded/bin/python3`` ＋
    ``lib/site-packages``（后者要在上一层找）。
    """
    bases = dict.fromkeys([python_exe.parent, python_exe.parent.parent])
    for base in bases:
        for name in ("Lib", "lib"):
            try:
                if (base / name / "site-packages" / "torch").is_dir():
                    return True
            except OSError:
                continue
    return False


def find_embedded_python(package_root: Path) -> Optional[Path]:
    """在便携包里找那套**真能干活**的 Python。

    找到候选还不够，必须确认它带了 torch。否则会出现「找到了 Python，
    一起服务就 ModuleNotFoundError: torch」这种更难懂的报错。
    """
    for name in EMBEDDED_DIR_ALIASES:
        for relative in (("python.exe",), ("bin", "python3"), ("bin", "python")):
            exe = package_root.joinpath(name, *relative)
            try:
                if exe.is_file() and has_torch(exe):
                    return exe
            except OSError:
                continue
    return None


def _extra_candidates() -> List[Path]:
    """便携包常见的落点。

    ``D:\\Works\\ACE-Step-1.5-portable`` 这类是实测中很常见的放法。另外做一层
    ``ACE-Step*`` 通配，兜住带版本号的目录名。
    """
    out: List[Path] = []

    bases = [Path.home()]
    for drive in ("C:", "D:", "E:", "F:"):
        anchor = Path(drive + os.sep)
        try:
            if anchor.is_dir():
                bases.append(anchor)
        except OSError:
            continue

    for base in bases:
        for suffix in EXTRA_ROOTS:
            try:
                out.append(base / suffix)
            except OSError:
                continue

    # 一层通配，兜住 ACE-Step-1.5 / ACE-Step-1.6 这类带版本号的目录
    for base in list(bases):
        try:
            if base.is_dir():
                out.extend(sorted(p for p in base.glob("ACE-Step*") if p.is_dir()))
        except OSError:
            continue

    return out


def discover_root(hint: Optional[str] = None) -> Optional[Path]:
    """找一个**能真起服务**的便携包根目录。

    判定要比 ``doctor.find_package_root`` 严一档：那边只要求目录结构像，
    这里还要求里面那套 Python 带了 torch。
    """

    def usable(candidate: Optional[Path]) -> Optional[Path]:
        if candidate is None:
            return None
        try:
            if candidate.is_dir() and find_embedded_python(candidate):
                return candidate.resolve()
        except OSError:
            return None
        return None

    from .doctor import find_package_root

    if hint:
        found = usable(Path(hint))
        if found:
            return found

    found = usable(find_package_root(hint))
    if found:
        return found

    for candidate in _extra_candidates():
        found = usable(candidate)
        if found:
            return found
    return None


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

    python_exe = find_embedded_python(root)
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
