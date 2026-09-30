# -*- coding: utf-8 -*-
"""环境自检。

本地跑 AI 写歌，真正会卡住人的从来不是「怎么调 API」，而是环境：
服务没起、模型没下、显存不够、或者撞上 Windows 上那个 triton 崩溃。

这个模块把这些检查项固化下来，一条命令跑完：

    sfs doctor
    sfs doctor --package-root D:\\Works\\ACE-Step-1.5-portable

检查分四级：

    OK      正常
    WARN    能用，但可能影响效果
    FAIL    不修就跑不了
    SKIP    缺少前提，跳过了

最后给一个整体结论和下一步该做什么。
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from .client import DEFAULT_BASE_URL, AceStepClient

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_FAIL = "fail"
LEVEL_SKIP = "skip"

LEVEL_LABELS = {
    LEVEL_OK: " OK ",
    LEVEL_WARN: "注意",
    LEVEL_FAIL: "失败",
    LEVEL_SKIP: "跳过",
}

# 打上补丁的 sitecustomize.py 里会带这个标记，doctor 用它判断补丁装没装。
PATCH_MARKER = "song-for-someone:triton-patch"

# 便携包里模型权重所在目录
CHECKPOINT_SUBDIR = "checkpoints"

# 关键模型目录名（下齐了才能出歌）
REQUIRED_CHECKPOINTS = ("acestep-v15-turbo", "acestep-5Hz-lm-1.7B")


@dataclass
class Check:
    """单项检查结果。"""

    key: str
    title: str
    level: str
    detail: str = ""
    fix: str = ""
    extra: List[str] = field(default_factory=list)


@dataclass
class DoctorReport:
    """整体自检报告。"""

    checks: List[Check] = field(default_factory=list)

    def add(
        self,
        key: str,
        title: str,
        level: str,
        detail: str = "",
        fix: str = "",
        extra: Optional[List[str]] = None,
    ) -> Check:
        check = Check(key, title, level, detail, fix, extra or [])
        self.checks.append(check)
        return check

    @property
    def failed(self) -> List[Check]:
        return [c for c in self.checks if c.level == LEVEL_FAIL]

    @property
    def warned(self) -> List[Check]:
        return [c for c in self.checks if c.level == LEVEL_WARN]

    @property
    def ok(self) -> bool:
        return not self.failed


# ------------------------------------------------------------------ 单项检查


def check_python(report: DoctorReport) -> None:
    version = sys.version_info
    text = f"{version.major}.{version.minor}.{version.micro}"
    if version >= (3, 9):
        report.add("python", "Python 版本", LEVEL_OK, f"{text} ｜ {platform.platform()}")
    else:
        report.add(
            "python", "Python 版本", LEVEL_FAIL,
            f"{text} 太旧",
            fix="本工具需要 Python 3.9 及以上。",
        )


def check_service(report: DoctorReport, base_url: str) -> None:
    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80

    # 先纯连接测试，把「端口不通」和「服务起来了但回非 200」区分开。
    reachable = False
    try:
        with socket.create_connection((host, port), timeout=2.0):
            reachable = True
    except OSError:
        reachable = False

    if not reachable:
        report.add(
            "service", "ACE-Step 服务", LEVEL_FAIL,
            f"{base_url} 端口不通",
            fix=(
                "启动服务：在 ACE-Step 便携包目录下运行 start_api_server.bat\n"
                "        如果服务已启动但端口不同，用 --base-url 指定正确地址。"
            ),
        )
        return

    client = AceStepClient(base_url, timeout=8.0)
    if client.health():
        report.add("service", "ACE-Step 服务", LEVEL_OK, f"{base_url} ／health 正常")
    else:
        report.add(
            "service", "ACE-Step 服务", LEVEL_WARN,
            f"{base_url} 端口能连上，但 /health 没返回正常结果",
            fix="服务可能还在初始化模型。稍等一会儿再试；持续如此请查看服务端日志。",
        )


def check_gpu(report: DoctorReport) -> None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        report.add(
            "gpu", "显卡", LEVEL_WARN,
            "没找到 nvidia-smi",
            fix="没有 N 卡也能跑，只是会落到 CPU 上，出歌速度会慢一个数量级。",
        )
        return

    try:
        output = subprocess.run(
            [
                exe,
                "--query-gpu=name,memory.total,memory.used,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
    except Exception as exc:
        report.add("gpu", "显卡", LEVEL_WARN, f"nvidia-smi 调用失败：{exc}")
        return

    if not output:
        report.add("gpu", "显卡", LEVEL_WARN, "nvidia-smi 没返回数据")
        return

    first = output.splitlines()[0]
    parts = [p.strip() for p in first.split(",")]
    if len(parts) < 4:
        report.add("gpu", "显卡", LEVEL_WARN, f"无法解析：{first}")
        return

    name, total, used, driver = parts[0], parts[1], parts[2], parts[3]
    try:
        total_mb = int(float(total))
        used_mb = int(float(used))
    except ValueError:
        total_mb = used_mb = 0

    detail = f"{name} ｜ 显存 {used_mb}/{total_mb} MiB ｜ 驱动 {driver}"

    # 显存门槛按上游 docs/zh/INSTALL.md：DiT 单独约 4GB，DiT+LM 约 6GB。
    if total_mb and total_mb < 6144:
        report.add(
            "gpu", "显卡", LEVEL_FAIL, detail,
            fix="显存不足 6GB。上游要求 DiT 与 LM 同时在卡上时至少 6GB，"
                "可以改用更小的 LM 型号，或走 CPU。",
        )
    elif total_mb and total_mb < 12288:
        report.add(
            "gpu", "显卡", LEVEL_WARN, detail,
            fix="6~12GB 属于刚好够用。把 batch_size 保持为 1，"
                "并避免同时让 DiT 和 LM 都常驻显存。",
        )
    else:
        report.add("gpu", "显卡", LEVEL_OK, detail)

    if used_mb and total_mb and used_mb / total_mb > 0.85:
        report.add(
            "gpu", "显存占用", LEVEL_WARN,
            f"已用 {used_mb}/{total_mb} MiB（{used_mb / total_mb:.0%}）",
            fix="别的程序占着显存。关掉浏览器里的 3D 页面或别的推理服务再跑。",
        )


# -------------------------------------------------------------- 便携包探测
#
# 探测逻辑只有这一份：起服务（engine）和报问题（doctor 各项检查）都从这里取
# 结果。之前两边各写了一套候选目录，慢慢漂移成「引擎自己找得到、自检说没有」，
# 而且自检那套只认 python_embeded 一个拼写，很难查。

# 便携包里那套 Python 所在目录的名字。上游 ``start_api_server.bat`` 判的是
# ``python_embedded``，实际解压出来却是 ``python_embeded``（少一个 n）。
# 两个拼写都认，免得跟着上游一起走错。
EMBEDDED_DIR_ALIASES = ("python_embeded", "python_embedded")

# Python 可执行文件在便携包里可能出现的位置：Windows 便携包直接放在目录下，
# 类 Unix 布局在 bin/ 里。
EMBEDDED_EXE_RELATIVES = (("python.exe",), ("bin", "python3"), ("bin", "python"))

# 便携包根目录里必须有的东西，用来确认「这确实是个便携包」。
PACKAGE_MARKER = "acestep"

# 便携包常见的落点，相对于用户主目录或各盘根目录。
EXTRA_ROOT_SUFFIXES = (
    "ACE-Step-1.5-portable",
    "Works/ACE-Step-1.5-portable",
    "AI/ACE-Step-1.5-portable",
    "Downloads/ACE-Step-1.5-portable",
)

# 用户在网页界面里手填过的便携包位置，记一行在仓库根的这个文件里。
# 下次启动直接用它，不用再填一遍。
REMEMBERED_ROOT_FILENAME = ".engine-root"


def _remembered_root_file() -> Path:
    """记住的位置存在哪个文件。测试可以改这个函数。"""
    return Path(__file__).resolve().parent.parent / REMEMBERED_ROOT_FILENAME


def remembered_root() -> Optional[Path]:
    """读回上次记住的便携包目录。没有 / 读不出 / 内容为空都返回 ``None``。"""
    try:
        text = _remembered_root_file().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        line = line.strip()
        if line:
            return Path(line)
    return None


def remember_root(root: Path) -> Optional[Path]:
    """把便携包目录记下来，返回落定后的路径；写不了返回 ``None``。

    只记路径，不校验 —— 校验是 ``validate_package_root`` 的事。写不进去
    （目录只读之类）也不该让启动失败，所以这里吞掉 OSError。
    """
    try:
        resolved = Path(root).resolve()
    except OSError:
        resolved = Path(root)
    try:
        _remembered_root_file().write_text(str(resolved) + "\n", encoding="utf-8")
    except OSError:
        return None
    return resolved


def forget_root() -> None:
    """忘掉记住的位置（文件本来就不在也无所谓）。"""
    try:
        _remembered_root_file().unlink()
    except OSError:
        pass


def _search_bases() -> List[Path]:
    """候选目录的搜索基点：当前目录、用户主目录、各盘根目录。"""
    bases: List[Path] = [Path.cwd(), Path.home()]
    for drive in ("C:", "D:", "E:", "F:"):
        anchor = Path(drive + os.sep)
        try:
            if anchor.is_dir():
                bases.append(anchor)
        except OSError:
            continue
    return bases


def candidate_roots(hint: Optional[str] = None) -> List[Path]:
    """按优先级列出便携包的候选目录，去重保序。

    顺序即优先级：显式指定的 > 界面上次记住的 > 当前目录及其上两级 >
    环境变量 > 常见落点 > 一层 ``ACE-Step*`` 通配。

    只做固定候选加一层通配，**不扫全盘** —— 扫盘会让自检和启动都卡住。
    """
    out: List[Path] = []

    if hint:
        out.append(Path(hint))

    # 界面上次手填过的位置，优先级仅次于本次显式指定
    remembered = remembered_root()
    if remembered is not None:
        out.append(remembered)

    here = Path.cwd()
    out.extend([here, here.parent, here.parent.parent])

    # 环境变量（用户可以在 .env 或系统变量里设）
    for env_key in ("ACESTEP_ROOT", "ACESTEP_HOME", "ACESTEP_PORTABLE_ROOT"):
        value = os.environ.get(env_key)
        if value:
            out.append(Path(value))

    bases = _search_bases()
    for base in bases:
        for suffix in EXTRA_ROOT_SUFFIXES:
            out.append(base / suffix)

    # 一层通配，兜住 ACE-Step-1.5 / ACE-Step-1.6 这类带版本号的目录名
    for base in bases:
        try:
            if base.is_dir():
                out.extend(sorted(p for p in base.glob("ACE-Step*") if p.is_dir()))
        except OSError:
            continue

    unique: List[Path] = []
    seen = set()
    for path in out:
        try:
            key = str(path.resolve()).lower()
        except OSError:
            key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def site_packages_of(python_exe: Path) -> List[Path]:
    """由 Python 可执行文件推出它的 site-packages 可能在哪。

    两种布局都要认：Windows 便携包是 ``python_embeded\\python.exe`` 配
    ``Lib\\site-packages``；类 Unix 是 ``bin/python3`` 配上一层的
    ``lib/site-packages``。所以 exe 的同级目录和上一级目录都得看。
    """
    out: List[Path] = []
    for base in dict.fromkeys([python_exe.parent, python_exe.parent.parent]):
        for name in ("Lib", "lib"):
            out.append(base / name / "site-packages")
    return out


def has_torch(python_exe: Path) -> bool:
    """这套 Python 的 site-packages 里有没有 torch。

    只看目录、不起子进程 —— 自检和启动路径上都不该为了它多花几秒去 import。
    """
    for site_packages in site_packages_of(python_exe):
        try:
            if (site_packages / "torch").is_dir():
                return True
        except OSError:
            continue
    return False


def find_embedded_python(
    package_root: Path,
    require_torch: bool = False,
) -> Optional[Path]:
    """在便携包里找那套 Python。

    :param require_torch: 只认**真能干活**的那套。起服务必须传 ``True``，
        否则会变成「找到了 Python，一起服务就 ModuleNotFoundError: torch」，
        比直接说找不到更难查。自检传 ``False``，好把「torch 缺失」当成一个
        具体问题报出来。
    """
    for name in EMBEDDED_DIR_ALIASES:
        for relative in EMBEDDED_EXE_RELATIVES:
            exe = package_root.joinpath(name, *relative)
            try:
                if exe.is_file() and (not require_torch or has_torch(exe)):
                    return exe
            except OSError:
                continue
    return None


def find_package_root(
    hint: Optional[str] = None,
    require_torch: bool = False,
) -> Optional[Path]:
    """定位 ACE-Step 便携包根目录。

    判定依据：该目录下有 ``acestep`` 包，并且能找到那套 Python。

    :param require_torch: 只返回能真起服务的那种（``engine`` 走这条）。自检用
        默认的 ``False``，好把「torch 缺失」单独报出来。
    """
    for candidate in candidate_roots(hint):
        try:
            if not candidate.is_dir():
                continue
            if not (candidate / PACKAGE_MARKER).is_dir():
                continue
            if find_embedded_python(candidate, require_torch=require_torch):
                return candidate.resolve()
        except OSError:
            continue
    return None


def find_site_packages(package_root: Path) -> Optional[Path]:
    """找到便携包内的 site-packages 目录。"""
    python_exe = find_embedded_python(package_root)
    if python_exe is None:
        return None
    for candidate in site_packages_of(python_exe):
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


def _check_python_of(root: Path) -> tuple:
    """便携包 ``root`` 里那套 Python 能不能用。返回 ``(root 或 None, 原因)``。"""
    python_exe = find_embedded_python(root, require_torch=False)
    if python_exe is None:
        return None, (
            f"{root} 里没找到 Python。\n"
            "解压完整的便携包里应该有一个 python_embeded 文件夹。"
        )
    if not has_torch(python_exe):
        try:
            label = str(python_exe.relative_to(root))
        except ValueError:
            label = str(python_exe)
        return None, (
            f"找到了 {label}，但它没装 torch，起不了服务。\n"
            "多半是上游脚本误用的那个 .venv 空壳（上游 start_api_server.bat 判的目录名\n"
            "比实际多一个 n）。重新解压一次便携包就好。"
        )
    return root, ""


def validate_package_root(raw: object) -> tuple:
    """检查用户手填的便携包路径。返回 ``(root 或 None, 原因)``。

    原因写给用户看，所以要说清「到底哪儿不对」而不是笼统一句「无效路径」。

    顺手容错两件事：路径首尾的引号与空格（从资源管理器「复制文件地址」常带），
    以及用户填成了上级目录（比如填 ``D:\\Works``，而便携包在它下面一层）。
    """
    text = str(raw if raw is not None else "").strip()
    # 资源管理器复制出来的是带引号的，用户也常把整句话粘进来
    text = text.strip('"').strip("'").strip()
    if not text:
        return None, "请先把便携包文件夹的完整路径粘进来。"

    path = Path(text)
    try:
        is_dir = path.is_dir()
    except OSError:
        is_dir = False
    if not is_dir:
        return None, (
            f"没有这个文件夹：{path}\n"
            "路径建议从资源管理器的地址栏复制；或者按住 Shift 右键那个文件夹，"
            "选「复制文件地址」。"
        )

    # 填的就是便携包本身
    if (path / PACKAGE_MARKER).is_dir():
        return _check_python_of(path)

    # 不是 —— 最常见的是填了上级目录，往下找一层
    try:
        children = sorted(p for p in path.iterdir() if p.is_dir())
    except OSError:
        children = []

    found = [child for child in children[:256] if (child / PACKAGE_MARKER).is_dir()]
    if len(found) == 1:
        return _check_python_of(found[0])
    if len(found) > 1:
        names = "、".join(child.name for child in found[:5])
        return None, (
            f"{path} 下面有好几个像便携包的文件夹（{names}…）。\n"
            "请把具体那一个的路径填进来。"
        )
    return None, (
        f"{path} 里没找到便携包。\n"
        f"解压出来的便携包，应该是一个里面同时有 {PACKAGE_MARKER} 和 "
        "python_embeded 两个文件夹的目录。请确认填的是它，而不是压缩包本身。"
    )


def check_package_root(report: DoctorReport, package_root: Optional[Path]) -> None:
    if package_root is None:
        report.add(
            "package", "ACE-Step 便携包", LEVEL_SKIP,
            "没找到便携包目录",
            fix=(
                "服务要是已经起起来了，这一项不影响出歌。\n"
                "         想让自检接着查 Python、triton 补丁和模型文件，用 --package-root 指一下，例如：\n"
                "         --package-root D:\\Works\\ACE-Step-1.5-portable\n"
                "         自检已经自动找过：当前目录及其上两级、用户主目录、各盘根目录，\n"
                "         以及这些目录下名字带 ACE-Step 的文件夹。"
            ),
        )
        return

    report.add("package", "ACE-Step 便携包", LEVEL_OK, str(package_root))


def check_embedded_python(report: DoctorReport, package_root: Optional[Path]) -> None:
    """检查便携包自带的那套 Python —— 真正跑模型的是它，不是上面那个。

    这里有个很容易踩的坑：便携包解压后可能同时躺着一个 uv 建的 ``.venv``，
    而它是个空壳（没装 torch）。上游 ``start_api_server.bat`` 判目录名时多写了
    一个 n，永远进不去 ``python_embeded`` 分支，于是退去用那个空壳，最后以
    ``ModuleNotFoundError: torch`` 收场。所以这一项要明确报出用的是哪一套。
    """
    if package_root is None:
        report.add(
            "embedded-python", "便携包自带 Python", LEVEL_SKIP,
            "没找到便携包，跳过了",
        )
        return

    python_exe = find_embedded_python(package_root)
    if python_exe is not None:
        try:
            label = str(python_exe.relative_to(package_root))
        except ValueError:
            label = str(python_exe)

        if has_torch(python_exe):
            report.add(
                "embedded-python", "便携包自带 Python", LEVEL_OK,
                f"{label} ｜ 带 torch",
            )
        else:
            report.add(
                "embedded-python", "便携包自带 Python", LEVEL_WARN,
                f"{label} 里没装 torch",
                fix=(
                    "这套 Python 起不了服务。多半是便携包没解压完整，\n"
                    "         重新解压一次官方便携包即可。"
                ),
            )
        return

    # 连个 Python 都没找到，看看是不是只剩那个 .venv 空壳
    if (package_root / ".venv").is_dir():
        report.add(
            "embedded-python", "便携包自带 Python", LEVEL_WARN,
            "只找到 .venv，那是个空壳",
            fix=(
                "上游的 start_api_server.bat 判的目录名是 python_embedded（多一个 n），\n"
                "         和实际解压出来的 python_embeded 对不上，于是它退去用 .venv —— \n"
                "         而 .venv 里没装 torch，服务起来就会报 ModuleNotFoundError。\n"
                "         解决办法：重新解压一次官方便携包；或者用 sfs 一键启动，\n"
                "         它自己会找对那套 Python。"
            ),
        )
        return

    report.add(
        "embedded-python", "便携包自带 Python", LEVEL_WARN,
        f"{package_root} 里没有 python_embeded 目录",
        fix="重新解压一次官方便携包。",
    )


def check_triton_patch(report: DoctorReport, package_root: Optional[Path]) -> None:
    """检查 Windows 上的 triton 崩溃补丁打了没有。"""
    if platform.system() != "Windows":
        report.add(
            "triton-patch", "triton 补丁", LEVEL_SKIP,
            "非 Windows 系统，不需要这个补丁",
        )
        return

    if package_root is None:
        report.add(
            "triton-patch", "triton 补丁", LEVEL_SKIP,
            "不知道便携包在哪，没法检查",
            fix="加 --package-root 参数后重跑。",
        )
        return

    site_packages = find_site_packages(package_root)
    if site_packages is None:
        report.add("triton-patch", "triton 补丁", LEVEL_SKIP, "找不到 site-packages")
        return

    patch_file = site_packages / "sitecustomize.py"
    if not patch_file.exists():
        report.add(
            "triton-patch", "triton 补丁", LEVEL_WARN,
            "没装（site-packages 下没有 sitecustomize.py）",
            fix=(
                "如果 diffusers 导入不报错，说明你的 triton 版本没这个问题，可以不管。\n"
                "         一旦遇到 WinError 5 / PermissionError 导致服务起不来，执行：\n"
                "         python tools/patch_windows_triton.py --package-root <便携包目录>"
            ),
        )
        return

    try:
        content = patch_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        report.add("triton-patch", "triton 补丁", LEVEL_WARN, f"读不了 {patch_file}：{exc}")
        return

    if PATCH_MARKER in content:
        report.add("triton-patch", "triton 补丁", LEVEL_OK, f"已装 ｜ {patch_file}")
    else:
        report.add(
            "triton-patch", "triton 补丁", LEVEL_WARN,
            f"{patch_file} 存在，但不是本工具装的",
            fix="如果它已经解决了 triton 的 PermissionError 就不用动；"
                "否则先备份再执行 patch_windows_triton.py。",
        )


def check_diffusers_import(report: DoctorReport, package_root: Optional[Path]) -> None:
    """直接验证 diffusers 能不能导入 —— 这是判断坑填没填上的最终证据。

    比读文件、比版本号都直接：能 import，就说明 triton 那条路是通的。
    """
    if package_root is None:
        report.add("diffusers", "diffusers 导入", LEVEL_SKIP, "不知道便携包在哪")
        return

    python_exe = find_embedded_python(package_root)
    if python_exe is None:
        report.add(
            "diffusers", "diffusers 导入", LEVEL_SKIP,
            "便携包里没找到可用的 Python",
        )
        return

    try:
        proc = subprocess.run(
            [str(python_exe), "-c",
             "import diffusers, torch; "
             "print(diffusers.__version__, torch.__version__, torch.cuda.is_available())"],
            capture_output=True, text=True, timeout=180,
            cwd=str(package_root),
        )
    except subprocess.TimeoutExpired:
        report.add("diffusers", "diffusers 导入", LEVEL_WARN, "导入超时（180 秒）")
        return
    except Exception as exc:
        report.add("diffusers", "diffusers 导入", LEVEL_WARN, f"调用失败：{exc}")
        return

    if proc.returncode == 0:
        parts = proc.stdout.strip().split()
        detail = proc.stdout.strip()
        if len(parts) >= 3:
            cuda = "CUDA 可用" if parts[2].lower() == "true" else "CUDA 不可用（会走 CPU）"
            detail = f"diffusers {parts[0]} ｜ torch {parts[1]} ｜ {cuda}"
        level = LEVEL_WARN if "False" in proc.stdout else LEVEL_OK
        report.add("diffusers", "diffusers 导入", level, detail)
    else:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        last = tail[-1] if tail else "(无输出)"
        report.add(
            "diffusers", "diffusers 导入", LEVEL_FAIL,
            f"导入失败：{last[:200]}",
            fix=(
                "如果报的是 WinError 5 / PermissionError，就是 triton 那个坑，执行：\n"
                "         python tools/patch_windows_triton.py --package-root <便携包目录>"
            ),
        )


def check_checkpoints(report: DoctorReport, package_root: Optional[Path]) -> None:
    """检查模型权重下全了没有。"""
    if package_root is None:
        report.add("checkpoints", "模型文件", LEVEL_SKIP, "不知道便携包在哪")
        return

    ckpt_dir = package_root / CHECKPOINT_SUBDIR
    if not ckpt_dir.is_dir():
        report.add(
            "checkpoints", "模型文件", LEVEL_FAIL,
            f"没有 {ckpt_dir}",
            fix="首次启动服务时会自动下载模型（约 10GB）。国内建议走 ModelScope 源。",
        )
        return

    missing = [name for name in REQUIRED_CHECKPOINTS if not (ckpt_dir / name).is_dir()]
    present = [name for name in REQUIRED_CHECKPOINTS if (ckpt_dir / name).is_dir()]

    if missing:
        report.add(
            "checkpoints", "模型文件", LEVEL_WARN,
            f"已有 {len(present)}/{len(REQUIRED_CHECKPOINTS)} ｜ 缺：{'、'.join(missing)}",
            fix="启动一次服务会自动补齐缺失的权重。",
        )
    else:
        size = dir_size_gb(ckpt_dir)
        report.add(
            "checkpoints", "模型文件", LEVEL_OK,
            f"{len(REQUIRED_CHECKPOINTS)}/{len(REQUIRED_CHECKPOINTS)} 齐 ｜ {ckpt_dir} ｜ 约 {size:.1f} GB",
        )


def dir_size_gb(path: Path) -> float:
    """目录大小（GB）。统计不出来就返回 0。"""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
    except OSError:
        return 0.0
    return total / (1024 ** 3)


def check_output_dir(report: DoctorReport, out_dir: Path) -> None:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        report.add(
            "output", "输出目录", LEVEL_FAIL,
            f"{out_dir} 不可写：{exc}",
            fix="换一个可写目录，或用 --out 指定别的路径。",
        )
        return

    try:
        usage = shutil.disk_usage(str(out_dir))
    except OSError:
        report.add("output", "输出目录", LEVEL_OK, str(out_dir))
        return

    free_gb = usage.free / (1024 ** 3)
    level = LEVEL_WARN if free_gb < 2 else LEVEL_OK
    report.add(
        "output", "输出目录", level,
        f"{out_dir} ｜ 剩余 {free_gb:.1f} GB",
        fix="单首歌约 2MB，但模型和缓存很吃盘。磁盘紧张时先腾点空间。" if level == LEVEL_WARN else "",
    )


# ------------------------------------------------------------------ 主流程


def run_doctor(
    base_url: str = DEFAULT_BASE_URL,
    package_root: Optional[str] = None,
    out_dir: Optional[Path] = None,
) -> DoctorReport:
    """跑完整自检。"""
    report = DoctorReport()
    resolved_root = find_package_root(package_root)

    check_python(report)
    check_service(report, base_url)
    check_gpu(report)
    check_package_root(report, resolved_root)
    check_embedded_python(report, resolved_root)
    check_triton_patch(report, resolved_root)
    check_diffusers_import(report, resolved_root)
    check_checkpoints(report, resolved_root)
    check_output_dir(report, out_dir or Path.cwd() / "songs")

    return report


def format_report(report: DoctorReport) -> str:
    """渲染自检报告。"""
    lines = ["环境自检", "=" * 62]

    for check in report.checks:
        label = LEVEL_LABELS.get(check.level, check.level)
        lines.append(f"[{label}] {check.title}")
        if check.detail:
            for line in check.detail.splitlines():
                lines.append(f"        {line}")
        for line in check.extra:
            lines.append(f"        {line}")
        if check.fix and check.level in (LEVEL_FAIL, LEVEL_WARN):
            for line in check.fix.splitlines():
                lines.append(f"        → {line}")
        lines.append("")

    lines.append("=" * 62)
    if report.ok and not report.warned:
        lines.append("结论：环境没问题，可以出歌了。")
        lines.append("下一步：  sfs styles          看内置风格模板")
    elif report.ok:
        lines.append(f"结论：可以出歌，但有 {len(report.warned)} 项提醒值得看一眼。")
    else:
        lines.append(f"结论：有 {len(report.failed)} 项必须先解决。")
        for check in report.failed:
            lines.append(f"  · {check.title}：{check.detail.splitlines()[0] if check.detail else ''}")
    return "\n".join(lines)
