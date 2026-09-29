#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给 Windows 上的 triton 3.3.1 打补丁 —— 修 FileCacheManager.put 的 PermissionError。

## 这是个什么问题

在 Windows 上跑 ACE-Step（以及任何用了 triton 的扩散模型管线），服务经常
起不来，报错长这样：

    PermissionError: [WinError 5] Access is denied: '...\\cuda_utils.pyd'

根因在 ``triton/runtime/cache.py`` 的 ``FileCacheManager.put()``：它把编译产物
先写到临时文件，再用 ``os.replace`` 换到最终位置。Windows 上 ``os.replace``
会因为目标文件被占用而失败；此时 put() 的兜底只 try 了 ``os.remove(temp_path)``，
而**刚编译出来的 ``cuda_utils.pyd`` 正被进程加载着锁死**，``remove`` 同样抛
``WinError 5``。异常于是冒出 put()，一路顶到调用方 —— 也就是 diffusers 的导入
链，服务直接起不来。

更烦的是它会自我维持：缓存文件永远写不进去，于是**每次启动都重新编译一遍**，
每次都在同一个地方失败。

## 为什么换缓存目录没用

``TRITON_CACHE_DIR`` 只是换个地方写，锁还在。脱离任何沙箱/杀软也一样 ——
这是真实的 Windows 文件锁语义，不是权限配置问题。

## 补丁做了什么

在 site-packages 下放一个 ``sitecustomize.py``（Python 启动时会自动 import），
把 ``FileCacheManager.put`` 包一层：遇到 ``PermissionError`` 时不再往上级抛，
而是直接把内容写到最终缓存路径，返回路径。

缓存内容是正确的，只是绕过了那个会失败的原子替换动作。下一次启动就能命中
缓存，不再重复编译。

## 用法

    # 自动探测便携包
    python tools/patch_windows_triton.py

    # 指定便携包目录
    python tools/patch_windows_triton.py --package-root D:\\Works\\ACE-Step-1.5-portable

    # 只看看装没装
    python tools/patch_windows_triton.py --check

    # 卸载
    python tools/patch_windows_triton.py --uninstall

脚本是幂等的：已装过再跑一次不会重复打；原目录里若有别人的
``sitecustomize.py``，会先备份再以标记块的形式追加，不动别人的内容。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

MARKER = "song-for-someone:triton-patch"
BEGIN_LINE = f"# --- BEGIN {MARKER} ---"
END_LINE = f"# --- END {MARKER} ---"

# 补丁主体。以标记块的形式追加到 sitecustomize.py。
PATCH_BLOCK = f'''{BEGIN_LINE}
# triton 3.3.1 / Windows: FileCacheManager.put 的 PermissionError 兜底。
#
# put() 在 os.replace 失败后只 try 了 os.remove(temp_path)；Windows 上刚编译出的
# cuda_utils.pyd 会被立即加载锁定，remove 同样抛 WinError 5，异常冒泡导致调用方
# （diffusers 等）导入失败，同时缓存永远填不满、每次启动重复编译。
# 本补丁包一层：遇 PermissionError 时把数据直接写入最终缓存路径并返回。
#
# 由 song-for-someone 的 tools/patch_windows_triton.py 安装，可安全卸载。
import os as _sfs_os


def _sfs_install_triton_patch():
    try:
        from triton.runtime import cache as _sfs_cache
    except Exception:
        # 这个环境没装 triton / triton 改了模块结构，静默跳过。
        return

    manager = getattr(_sfs_cache, "FileCacheManager", None)
    if manager is None:
        return

    original_put = manager.put
    if getattr(original_put, "_sfs_patched", False):
        # 已经打过了，别重复包（重复包会让调用链一层层套上去）。
        return

    def _sfs_safe_put(self, data, filename, binary=True):
        try:
            return original_put(self, data, filename, binary)
        except PermissionError:
            # 非 Windows 上不该出现这个错误，原样抛出，免得掩盖真实问题。
            if _sfs_os.name != "nt":
                raise
            filepath = self._make_path(filename)
            try:
                if not _sfs_os.path.exists(filepath):
                    if isinstance(data, bytes):
                        with open(filepath, "wb") as handle:
                            handle.write(data)
                    else:
                        with open(filepath, "w") as handle:
                            handle.write(str(data))
            except Exception:
                # 兜底失败就算了，返回路径让上层继续；总比直接崩掉好。
                pass
            return filepath

    _sfs_safe_put._sfs_patched = True
    manager.put = _sfs_safe_put


_sfs_install_triton_patch()
{END_LINE}
'''


def find_site_packages(explicit: Optional[str], package_root: Optional[str]) -> Optional[Path]:
    """按 显式指定 → 便携包推导 → 当前解释器 的顺序找 site-packages。"""
    if explicit:
        path = Path(explicit)
        return path if path.is_dir() else None

    if package_root:
        root = Path(package_root)
        for candidate in (
            root / "python_embeded" / "Lib" / "site-packages",
            root / "python_embeded" / "lib" / "site-packages",
        ):
            if candidate.is_dir():
                return candidate
        return None

    # 从当前解释器里找（适用于自己建的 venv）
    for entry in sys.path:
        if not entry:
            continue
        path = Path(entry)
        if path.name == "site-packages" and path.is_dir():
            return path
    return None


def read_patch_state(target: Path) -> str:
    """返回 'absent' / 'installed' / 'other'。"""
    if not target.exists():
        return "absent"
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "other"
    if BEGIN_LINE in content and END_LINE in content:
        return "installed"
    if MARKER in content:
        return "installed"
    return "other"


def guess_python_exe(site_packages: Path) -> Optional[Path]:
    """从 site-packages 往上推便携包里的 python.exe。

    便携包结构是 ``<root>/python_embeded/Lib/site-packages``，
    所以 ``python.exe`` 在 site-packages 往上两级。
    """
    for levels_up in (site_packages.parent.parent, site_packages.parent.parent.parent):
        candidate = levels_up / "python.exe"
        if candidate.exists():
            return candidate
    return None


def install(target: Path, dry_run: bool = False) -> int:
    state = read_patch_state(target)

    if state == "installed":
        print(f"已经装过了，跳过：{target}")
        print("（脚本是幂等的，重复执行不会出问题。）")
        return 0

    if state == "other":
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = target.with_name(f"{target.name}.bak-{stamp}")
        print(f"注意：{target} 已经存在，且不是本工具装的。")
        print(f"      会先备份到 {backup}，再以标记块的形式追加，不动原有内容。")
        if not dry_run:
            shutil.copy2(target, backup)

    print(f"目标：{target}")
    if dry_run:
        print("--dry-run：不写文件。")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(target, "a", encoding="utf-8", newline="\n") as handle:
            if target.exists() and target.stat().st_size > 0:
                handle.write("\n\n")
            handle.write(PATCH_BLOCK)
    except OSError as exc:
        print(f"写入失败：{exc}", file=sys.stderr)
        print("可能是文件被占用或权限不足。关掉正在运行的 ACE-Step 服务后重试。",
              file=sys.stderr)
        return 1

    print("已写入补丁。")
    print()

    python_exe = guess_python_exe(target.parent)
    if python_exe:
        print("验证方式（用便携包自带的解释器）：")
        print(f'  "{python_exe}" -c "import diffusers; print(\'diffusers OK\')"')
        print()
        print("补丁只在 Python 启动时生效 —— 正在跑的进程不受影响，下次启动才用上新补丁。")
    else:
        print("验证方式：用装了 triton 的那个 Python 跑一句：")
        print('  python -c "import diffusers; print(\'diffusers OK\')"')
    return 0


def uninstall(target: Path, dry_run: bool = False) -> int:
    if not target.exists():
        print(f"没有这个文件：{target}")
        return 0

    try:
        content = target.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"读不了 {target}：{exc}", file=sys.stderr)
        return 1

    if BEGIN_LINE not in content:
        print(f"{target} 里没有本工具打的补丁，未改动。")
        return 0

    start = content.index(BEGIN_LINE)
    end = content.index(END_LINE) + len(END_LINE)
    remainder = (content[:start] + content[end:]).rstrip()

    print(f"目标：{target}")
    if dry_run:
        print("--dry-run：不写文件。")
        return 0

    try:
        if remainder.strip():
            target.write_text(remainder + "\n", encoding="utf-8")
            print("已移除补丁块（文件里还有别的内容，已保留）。")
        else:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = target.with_name(f"{target.name}.removed-{stamp}")
            shutil.move(str(target), str(backup))
            print("已移除补丁。文件里原本只有补丁内容，实体已改名为：")
            print(f"  {backup}")
    except OSError as exc:
        print(f"写入失败：{exc}", file=sys.stderr)
        return 1
    return 0


def check(target: Path) -> int:
    state = read_patch_state(target)
    print(f"检查目标：{target}")
    if not target.exists():
        print()
        print("结果：未安装（文件不存在）")
        return 1
    if state == "installed":
        print()
        print("结果：已安装")
        return 0
    print()
    print("结果：文件存在，但里面没有本工具的补丁")
    print("      如果它已经解决了 triton 的 PermissionError，就不用再动；")
    print("      否则直接跑一次不带参数的 install —— 会先备份再以标记块追加。")
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="patch_windows_triton.py",
        description="给 Windows 上的 triton 3.3.1 打 FileCacheManager.put 补丁。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--package-root", default=None,
                        help="ACE-Step 便携包根目录，如 D:\\Works\\ACE-Step-1.5-portable")
    parser.add_argument("--site-packages", default=None,
                        help="直接指定 site-packages 目录（优先于 --package-root）")
    parser.add_argument("--check", action="store_true", help="只检查，不修改")
    parser.add_argument("--uninstall", action="store_true", help="卸载补丁")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要做什么")
    args = parser.parse_args(argv)

    if sys.platform != "win32" and not args.check:
        print("这个补丁是给 Windows 用的。当前系统不是 Windows。")
        print("如果只是想确认状态，加 --check。")
        return 0

    site_packages = find_site_packages(args.site_packages, args.package_root)
    if site_packages is None:
        print("没找到 site-packages 目录。", file=sys.stderr)
        print("请用 --package-root 指定 ACE-Step 便携包目录，例如：", file=sys.stderr)
        print("  python tools/patch_windows_triton.py "
              "--package-root D:\\Works\\ACE-Step-1.5-portable", file=sys.stderr)
        print("或用 --site-packages 直接指定目录。", file=sys.stderr)
        return 2

    target = site_packages / "sitecustomize.py"

    if args.check:
        return check(target)
    if args.uninstall:
        return uninstall(target, dry_run=args.dry_run)
    return install(target, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
