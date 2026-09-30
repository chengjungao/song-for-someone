# -*- coding: utf-8 -*-
"""命令行入口。

    sfs doctor                    环境自检
    sfs check lyrics.txt          歌词体检
    sfs styles                    列出风格模板
    sfs style folk                看某个模板的详情
    sfs make --style folk ...     出歌
    sfs songs                     看已经出过哪些歌

也可以不安装直接跑：:

    python -m song_for_someone doctor
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import __version__
from .client import (
    DEFAULT_BASE_URL,
    STATUS_RUNNING,
    AceStepClient,
    AceStepError,
    GenerateRequest,
    ServiceUnreachable,
    first_meta,
)
from .common import default_out_name, human_duration, setup_console, write_sidecar
from .doctor import format_report as format_doctor_report, run_doctor
from .lyrics import analyze as analyze_lyrics
from .lyrics import format_report as format_lyrics_report
from .styles import format_preset_detail, format_preset_table, get_preset, list_presets

DEFAULT_OUT_DIR = "songs"
DEFAULT_TIMEOUT = 1800.0


# ------------------------------------------------------------------ 工具


def read_lyrics(args: argparse.Namespace) -> str:
    """按命令行参数取歌词文本。"""
    if args.lyrics is not None:
        return args.lyrics
    if args.lyrics_file:
        path = Path(args.lyrics_file)
        if not path.exists():
            raise SystemExit(f"找不到歌词文件：{path}")
        return path.read_text(encoding="utf-8")
    if args.instrumental:
        return "[Instrumental]"
    raise SystemExit(
        "没有歌词。请用 --lyrics-file 指定文件、--lyrics 直接给文本，\n"
        "或者加 --instrumental 出一首纯音乐。"
    )


def guard_instrumental(lyrics: str, instrumental: bool) -> str:
    """--instrumental 的语义：把带人声的歌词换成纯器乐标记。"""
    if instrumental:
        return "[Instrumental]"
    return lyrics


# ------------------------------------------------------------------ 子命令


def cmd_doctor(args: argparse.Namespace) -> int:
    report = run_doctor(
        base_url=args.base_url,
        package_root=args.package_root,
        out_dir=Path(args.out_dir),
    )
    print(format_doctor_report(report))
    return 0 if report.ok else 1


def cmd_check(args: argparse.Namespace) -> int:
    path = Path(args.lyrics_file)
    if not path.exists():
        print(f"找不到歌词文件：{path}", file=sys.stderr)
        return 2

    lyrics = path.read_text(encoding="utf-8")
    report = analyze_lyrics(lyrics)

    if args.json:
        payload = {
            "total_chars": report.total_chars,
            "total_lines": report.total_lines,
            "cjk_ratio": round(report.cjk_ratio, 3),
            "has_structure": report.has_structure,
            "ok": report.ok,
            "sections": [
                {
                    "index": s.index,
                    "tag": s.tag,
                    "kind": s.kind,
                    "lines": len(s.lines),
                    "max_line_width": s.max_line_width,
                }
                for s in report.sections
            ],
            "issues": [
                {"level": i.level, "code": i.code, "message": i.message,
                 "section_index": i.section_index}
                for i in report.issues
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_lyrics_report(report))

    return 0 if report.ok else 1


def cmd_styles(args: argparse.Namespace) -> int:
    print(format_preset_table())
    return 0


def cmd_style(args: argparse.Namespace) -> int:
    preset = get_preset(args.key)
    if preset is None:
        print(f"没有这个模板：{args.key}", file=sys.stderr)
        print("可用的有：" + "、".join(p.key for p in list_presets()), file=sys.stderr)
        return 2
    print(format_preset_detail(preset))
    return 0


def cmd_make(args: argparse.Namespace) -> int:
    # --- 风格 ---
    preset = None
    caption = args.caption or ""

    if args.style:
        preset = get_preset(args.style)
        if preset is None:
            print(f"没有这个风格模板：{args.style}", file=sys.stderr)
            print("用 sfs styles 看全部可用模板。", file=sys.stderr)
            return 2
        if not caption:
            caption = preset.caption

    if not caption:
        print(
            "没有风格描述。用 --style 选内置模板，或用 --caption 直接写一段。\n"
            "风格描述建议按「乐器 + 人声 + 情绪 + 速度 + 副歌走向」来写。",
            file=sys.stderr,
        )
        return 2

    # --- 歌词 ---
    try:
        lyrics = read_lyrics(args)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    lyrics = guard_instrumental(lyrics, args.instrumental)

    duration = args.duration
    if duration is None:
        duration = preset.duration if preset else 120.0
    language = args.lang or (preset.vocal_language if preset else "zh")

    # --- 歌词体检 ---
    report = analyze_lyrics(lyrics)
    if not args.quiet:
        print(format_lyrics_report(report))
        print()

    if report.errors and not args.force:
        print("歌词有错误项，已中止。改完再跑，或加 --force 强行生成。", file=sys.stderr)
        return 3
    if report.warnings and not args.yes and not args.force:
        print("歌词有警告项。加 --yes 确认继续。", file=sys.stderr)
        return 3

    # --- 提交 ---
    request = GenerateRequest(
        prompt=caption,
        lyrics=lyrics,
        thinking=not args.no_thinking,
        vocal_language=language,
        audio_duration=duration,
        inference_steps=args.steps,
        batch_size=args.batch,
        audio_format=args.format,
        seed=args.seed,
        bpm=args.bpm,
        key_scale=args.key or "",
        time_signature=args.time_signature or "",
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_name = args.out or default_out_name()
    if not out_name.lower().endswith(("." + args.format, ".mp3", ".wav", ".flac")):
        out_name = f"{out_name}.{args.format}"
    out_path = out_dir / out_name

    client = AceStepClient(args.base_url, timeout=args.http_timeout)

    if args.dry_run:
        print("--dry-run：只体检不生成。将提交的请求：")
        print(json.dumps(request.to_payload(), ensure_ascii=False, indent=2))
        print(f"\n输出会写到：{out_path}")
        return 0

    print(f"风格： {caption}")
    print(f"时长： {human_duration(duration)} ｜ 语言：{language} ｜ "
          f"步数：{args.steps} ｜ 版本数：{args.batch}")
    print(f"输出： {out_path}")
    print()

    started = time.time()
    try:
        task_id = client.submit(request)
    except ServiceUnreachable as exc:
        print(f"\n{exc}", file=sys.stderr)
        print("先跑 sfs doctor 看看环境。", file=sys.stderr)
        return 4
    except AceStepError as exc:
        print(f"\n提交失败：{exc}", file=sys.stderr)
        return 4

    print(f"任务已提交：{task_id}")
    print()

    def on_progress(elapsed: float, status: int) -> None:
        marker = "." if status == STATUS_RUNNING else "?"
        print(f"  [{elapsed:6.0f}s] 生成中 {marker}", flush=True)

    try:
        results = client.wait(
            task_id, timeout=args.timeout, interval=args.interval, on_progress=on_progress
        )
    except AceStepError as exc:
        print(f"\n生成失败：{exc}", file=sys.stderr)
        return 5

    elapsed = time.time() - started
    print()
    print(f"生成完成，用时 {elapsed:.1f} 秒。")
    print()

    # --- 下载 ---
    saved: List[Path] = []
    for index, result in enumerate(results):
        if len(results) == 1:
            target = out_path
        else:
            target = out_path.with_name(f"{out_path.stem}_{index}{out_path.suffix}")
        try:
            client.download(result.file_url, target)
        except AceStepError as exc:
            print(f"  下载失败：{exc}", file=sys.stderr)
            continue
        size_mb = target.stat().st_size / (1024 * 1024)
        print(f"  {target} ｜ {size_mb:.1f} MB")
        if result.describe():
            print(f"    {result.describe()}")
        saved.append(target)

    if not saved:
        print("没有文件被保存下来。", file=sys.stderr)
        return 5

    # --- 留一份记录，方便复现 ---
    write_sidecar(out_path, request, results, elapsed, preset)

    print()
    print(f"共 {len(saved)} 个文件，用时 {elapsed:.1f} 秒。")
    print(f"记录写在：{out_path.with_suffix('.json')}")
    return 0


def cmd_songs(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir)
    if not out_dir.is_dir():
        print(f"还没有输出目录：{out_dir}")
        print("出第一首歌以后就会有了。")
        return 0

    audio_files = sorted(
        [p for p in out_dir.iterdir()
         if p.suffix.lower() in (".mp3", ".wav", ".flac")],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not audio_files:
        print(f"{out_dir} 里还没有歌。")
        return 0

    print(f"{out_dir} 里共 {len(audio_files)} 个文件")
    print("=" * 62)
    for path in audio_files:
        size_mb = path.stat().st_size / (1024 * 1024)
        stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%m-%d %H:%M")
        line = f"  {stamp}  {size_mb:>5.1f} MB  {path.name}"

        sidecar = path.with_suffix(".json")
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
                results = meta.get("results") or []
                if results:
                    metas = results[0].get("metas") or {}
                    bits = []
                    bpm = first_meta(metas, "bpm")
                    if bpm:
                        bits.append(f"{bpm} BPM")
                    key = first_meta(metas, "keyscale", "key_scale")
                    if key:
                        bits.append(str(key))
                    if bits:
                        line += "  ｜ " + " · ".join(bits)
            except Exception:
                pass
        print(line)
    return 0


# ------------------------------------------------------------------ 参数解析


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sfs",
        description="本地 AI 写歌工具箱 —— 给某个人的一首歌。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  sfs doctor\n"
            "  sfs styles\n"
            "  sfs check lyrics.txt\n"
            "  sfs make --style folk --lyrics-file lyrics.txt -o 给妈妈的歌.mp3\n"
            "  sfs make --style birthday --lyrics \"...\" --instrumental -d 90\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"sfs {__version__}")

    sub = parser.add_subparsers(dest="command")

    # doctor
    p_doctor = sub.add_parser("doctor", help="环境自检")
    p_doctor.add_argument("--base-url", default=DEFAULT_BASE_URL, help="服务地址")
    p_doctor.add_argument("--package-root", default=None, help="ACE-Step 便携包目录")
    p_doctor.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="输出目录")
    p_doctor.set_defaults(func=cmd_doctor)

    # check
    p_check = sub.add_parser("check", help="歌词体检")
    p_check.add_argument("lyrics_file", help="歌词文件路径")
    p_check.add_argument("--json", action="store_true", help="输出 JSON")
    p_check.set_defaults(func=cmd_check)

    # styles
    p_styles = sub.add_parser("styles", help="列出风格模板")
    p_styles.set_defaults(func=cmd_styles)

    # style
    p_style = sub.add_parser("style", help="查看某个风格模板")
    p_style.add_argument("key", help="模板键名，如 folk")
    p_style.set_defaults(func=cmd_style)

    # make
    p_make = sub.add_parser("make", help="出歌")
    p_make.add_argument("-s", "--style", help="内置风格模板键名，如 folk")
    p_make.add_argument("-c", "--caption", help="直接指定风格描述（优先于 --style）")
    p_make.add_argument("-l", "--lyrics", default=None, help="直接给歌词文本")
    p_make.add_argument("-f", "--lyrics-file", default=None, help="从文件读歌词")
    p_make.add_argument("--instrumental", action="store_true", help="出纯音乐，忽略歌词")
    p_make.add_argument("-d", "--duration", type=float, default=None, help="时长（秒）")
    p_make.add_argument("-o", "--out", default=None, help="输出文件名")
    p_make.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="输出目录")
    p_make.add_argument("--seed", type=int, default=None, help="随机种子（复现同一首）")
    p_make.add_argument("-b", "--batch", type=int, default=1, help="一次出几版")
    p_make.add_argument("--steps", type=int, default=8, help="扩散步数，默认 8")
    p_make.add_argument("--format", default="mp3", choices=["mp3", "wav", "flac"])
    p_make.add_argument("--lang", default=None, help="人声语言，中文为 zh")
    p_make.add_argument("--bpm", type=int, default=None, help="指定 BPM，不给则模型自判")
    p_make.add_argument("--key", default=None, help="指定调式，如 G major")
    p_make.add_argument("--time-signature", dest="time_signature", default=None, help="拍号，如 4/4")
    p_make.add_argument("--no-thinking", action="store_true", help="关掉 planner 思考，更快")
    p_make.add_argument("--base-url", default=DEFAULT_BASE_URL, help="服务地址")
    p_make.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="生成总超时（秒）")
    p_make.add_argument("--interval", type=float, default=5.0, help="轮询间隔（秒）")
    p_make.add_argument("--http-timeout", type=float, default=60.0, help="单次 HTTP 超时（秒）")
    p_make.add_argument("-y", "--yes", action="store_true", help="有警告也直接继续")
    p_make.add_argument("--force", action="store_true", help="有错误也强行生成")
    p_make.add_argument("-q", "--quiet", action="store_true", help="不打印歌词体检报告")
    p_make.add_argument("--dry-run", action="store_true", help="只体检，不真的生成")
    p_make.set_defaults(func=cmd_make)

    # songs
    p_songs = sub.add_parser("songs", help="看已经出过哪些歌")
    p_songs.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help="输出目录")
    p_songs.set_defaults(func=cmd_songs)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    setup_console()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
