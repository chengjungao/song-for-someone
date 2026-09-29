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


def find_package_root(hint: Optional[str] = None) -> Optional[Path]:
    """定位 ACE-Step 便携包根目录。

    判定依据：该目录下同时存在 ``python_embeded`` 和 ``acestep``。
    """
    candidates: List[Path] = []

    if hint:
        candidates.append(Path(hint))

    # 当前目录及其上两级
    here = Path.cwd()
    candidates.extend([here, here.parent, here.parent.parent])

    # 环境变量（用户可以在 .env / 系统变量里设）
    for env_key in ("ACESTEP_ROOT", "ACESTEP_HOME", "ACESTEP_PORTABLE_ROOT"):
        value = os.environ.get(env_key)
        if value:
            candidates.append(Path(value))

    for candidate in candidates:
        try:
            if (candidate / "python_embeded").is_dir() and (candidate / "acestep").is_dir():
                return candidate.resolve()
        except OSError:
            continue
    return None


def find_site_packages(package_root: Path) -> Optional[Path]:
    """找到便携包内的 site-packages 目录。"""
    for candidate in (
        package_root / "python_embeded" / "Lib" / "site-packages",
        package_root / "python_embeded" / "lib" / "site-packages",
    ):
        if candidate.is_dir():
            return candidate
    return None


def check_package_root(report: DoctorReport, package_root: Optional[Path]) -> None:
    if package_root is None:
        report.add(
            "package", "ACE-Step 便携包", LEVEL_SKIP,
            "没找到便携包目录",
            fix=(
                "如果服务已经起起来了，这一项无所谓。\n"
                "         要检查 triton 补丁和模型文件，用 --package-root 指定目录，\n"
                "         例如： --package-root D:\\Works\\ACE-Step-1.5-portable"
            ),
        )
        return

    report.add("package", "ACE-Step 便携包", LEVEL_OK, str(package_root))

    site_packages = find_site_packages(package_root)
    if site_packages is None:
        report.add(
            "package", "site-packages 目录", LEVEL_WARN,
            f"{package_root} 下没找到 site-packages",
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

    python_exe = package_root / "python_embeded" / "python.exe"
    if not python_exe.exists():
        report.add("diffusers", "diffusers 导入", LEVEL_SKIP, "便携包里没有 python.exe")
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
