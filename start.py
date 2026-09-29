# -*- coding: utf-8 -*-
"""跨平台双击入口。

双击这个文件（或在终端里 ``python start.py``），会启动本地网页界面，
并在就绪后自动打开浏览器。关掉窗口即停止。

它不装任何东西 —— 只要求本机有一个 Python 3.9 及以上。
"""

from __future__ import annotations

import os
import sys

MIN_VERSION = (3, 9)


def _ensure_package_importable() -> None:
    """让脚本无论在哪个工作目录被双击，都能 import 到本项目的包。"""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)


def _pause() -> None:
    """双击运行时，让窗口停住，错误才看得见。"""
    try:
        input("按回车键关闭…")
    except (EOFError, KeyboardInterrupt):
        pass


def main() -> int:
    """校验 Python 版本并拉起网页服务。"""
    if sys.version_info < MIN_VERSION:
        current = f"{sys.version_info.major}.{sys.version_info.minor}"
        print("=" * 60)
        print(f"  你的 Python 是 {current}，太旧了。")
        print("  这个程序需要 Python 3.9 或更高版本。")
        print()
        print("  去这里下载新版（免费）：")
        print("    https://www.python.org/downloads/")
        print("  安装时请务必勾选「Add Python to PATH」。")
        print("=" * 60)
        _pause()
        return 1

    _ensure_package_importable()

    try:
        from song_for_someone.web import main as web_main

        # 把多余的命令行参数透传给 web.main（例如 --no-browser、--port）。
        # 双击运行时没有参数，行为不变：默认自动开浏览器。
        return web_main(sys.argv[1:])
    except KeyboardInterrupt:
        print("\n已停止。")
        return 0
    except Exception as exc:  # noqa: BLE001 —— 双击入口，绝不把用户直接甩给 traceback
        print()
        print("启动失败。请把下面这段文字完整发给作者，方便定位问题：")
        print("-" * 60)
        print(f"  {type(exc).__name__}: {exc}")
        print("-" * 60)
        import traceback

        traceback.print_exc()
        _pause()
        return 1


if __name__ == "__main__":
    sys.exit(main())
