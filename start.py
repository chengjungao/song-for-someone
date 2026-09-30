# -*- coding: utf-8 -*-
"""跨平台双击入口。

双击这个文件（或在终端里 ``python start.py``）会做两件事：

1. 确认音乐引擎（ACE-Step 本地服务）在跑；不在跑，就用便携包自带的那套 Python 拉起来
2. 启动本地网页界面，并在就绪后自动打开浏览器

先备引擎是有意的。否则用户填完歌词点了出歌，才发现后端根本没起。

**引擎跟着这个脚本走**：窗口关掉（或按 Ctrl+C），界面和引擎一起停，约 14GB 显存
随即释放。想让引擎长留着、省掉每次约 1 分钟的冷启动，就用便携包里的
`启动引擎.bat` 单独开着它，这里检测到已有引擎会直接复用。

它不装任何东西 —— 只要求本机有一个 Python 3.9 及以上。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Tuple

MIN_VERSION = (3, 9)
RULE = "=" * 62


def _ensure_package_importable() -> None:
    """让脚本无论在哪个工作目录被双击，都能 import 到本项目的包。"""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)


def _say(line: str = "") -> None:
    """打印一行并立刻刷出去。

    双击运行时控制台是唯一的反馈通道，不能攒在缓冲里让人干等。
    """
    print(line, flush=True)


def _pause() -> None:
    """双击运行时，让窗口停住，错误才看得见。"""
    try:
        input("按回车键关闭…")
    except (EOFError, KeyboardInterrupt):
        pass


def _parse_args(argv: List[str]) -> Tuple[argparse.Namespace, List[str]]:
    """分出本脚本自己的参数，其余原样留给网页服务。

    这里用 ``parse_known_args``：本脚本只认引擎相关的几个开关，
    ``--host`` / ``--port`` / ``--out-dir`` 那些一律透传给网页服务。
    """
    parser = argparse.ArgumentParser(
        prog="start.py",
        description="一键启动：先把音乐引擎带起来，再打开网页界面。",
    )
    parser.add_argument(
        "--no-engine", action="store_true", help="不自动启动音乐引擎，只开界面"
    )
    parser.add_argument(
        "--engine-root", default=None, help="ACE-Step 便携包目录（不给就自动找）"
    )
    parser.add_argument(
        "--base-url", default=None, help="ACE-Step 服务地址（默认 http://127.0.0.1:8001）"
    )
    known, rest = parser.parse_known_args(argv)

    # --base-url 我们自己要用（探测和拉起引擎都得知道地址），
    # 网页服务也要用，所以再塞回去一份。
    passthrough = list(rest)
    if known.base_url:
        passthrough += ["--base-url", known.base_url]
    return known, passthrough


def _prepare_engine(
    base_url: str, engine_root: Optional[str], enabled: bool
) -> Optional[object]:
    """把音乐引擎准备好，返回「我们自己起的」那个句柄（没有就 None）。

    这里的任何失败都不该挡住界面 —— 界面里还有环境检查可以看问题在哪。
    返回值很关键：只有我们自己起的引擎才由我们负责停；复用别人起的（或者
    用户已经自己开着的）绝不能顺手关掉。
    """
    _say("[1/2] 音乐引擎")

    if not enabled:
        _say("  按 --no-engine 跳过。")
        _say()
        return None

    from song_for_someone.engine import STARTED, ensure_engine

    outcome = ensure_engine(
        base_url=base_url,
        root_hint=engine_root,
        log=lambda line: _say("  " + line),
    )

    if outcome.ready:
        _say()
        return outcome.handle if outcome.status == STARTED else None

    _say()
    for line in outcome.message.splitlines():
        _say("  " + line)
    _say()
    _say("  界面照常打开，可以先用界面里的环境检查看看到底缺什么。")
    _say()
    return None


def _stop_engine(handle: Optional[object]) -> None:
    """停掉我们自己起的引擎，把显存还回去。"""
    if handle is None:
        return

    from song_for_someone.engine import stop_engine

    _say()
    _say("正在停止音乐引擎，释放显存…")
    if stop_engine(handle):  # type: ignore[arg-type]
        _say("引擎已停止。")
    else:
        _say("引擎没能正常停止。可以在任务管理器里结束那个 python 进程。")


def main() -> int:
    """校验 Python 版本，备好引擎，拉起网页服务。"""
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

    args, web_args = _parse_args(sys.argv[1:])

    from song_for_someone.client import DEFAULT_BASE_URL

    base_url = args.base_url or DEFAULT_BASE_URL

    _say()
    _say(RULE)
    _say("  song-for-someone ｜ 给某个人的一首歌")
    _say(RULE)
    _say()

    engine_handle = None
    try:
        try:
            engine_handle = _prepare_engine(
                base_url, args.engine_root, not args.no_engine
            )
        except KeyboardInterrupt:
            _say()
            _say("已取消。")
            return 130
        except Exception as exc:  # noqa: BLE001 —— 引擎环节不该挡住界面
            _say(f"  准备引擎时出错：{type(exc).__name__}: {exc}")
            _say("  界面仍会打开。")
            _say()

        _say("[2/2] 网页界面")

        try:
            from song_for_someone.web import main as web_main
            from song_for_someone.web import set_engine_handle

            # 把句柄交给界面：环境页那个「停止出歌程序」要停的就是这一个。
            # 复用别人起的引擎时 engine_handle 是 None，界面那边会如实说「停不了」，
            # 而不是显示一个按了没反应的按钮。
            set_engine_handle(engine_handle)

            # 把多余的命令行参数透传给 web.main（例如 --port、--no-browser）。
            return web_main(web_args)
        except KeyboardInterrupt:
            _say()
            _say("界面已停止。")
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
    finally:
        # 界面停了就把我们自己起的引擎也停掉，别让 14GB 显存白白占着。
        try:
            _stop_engine(engine_handle)
        except Exception as exc:  # noqa: BLE001 —— 收尾出错不该盖住原始结果
            _say(f"  停止引擎时出错：{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
