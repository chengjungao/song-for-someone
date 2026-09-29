# -*- coding: utf-8 -*-
"""命令行与网页界面共用的工具函数。

这些函数原本住在 ``cli.py`` 里。网页界面（``web.py``）也要用同一套
「输出文件名」「时长的人话」「复现记录」逻辑。为了不打破 ``cli.py`` 的对外
行为，把定义搬到这里，``cli.py`` 改为 ``from .common import ...``。

依赖方向（禁止反向依赖）：本模块**绝不 import** ``cli``。仅允许引用包内的
``client`` 与 ``styles``（而且只用于类型标注），因此不会产生循环依赖。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from .client import GenerateRequest
from .styles import StylePreset

# 输出目录的默认名。CLI 的 ``--out-dir`` 默认值与它同名同值。
DEFAULT_OUT_DIR = "songs"

# 单段文件名最多保留多少个字符（保留中文，按字符数算）。
MAX_FILENAME_PART_CHARS = 32

# Windows 上不允许单独用作文件名的保留名。
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# 文件名里不能出现的字符 —— 按 Windows 最严的规矩来，跨平台都安全。
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')

# 控制字符：\x00-\x1f 与 DEL（\x7f）。
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

# 连续空白（含换行、制表符）。
_WHITESPACE_RUN = re.compile(r"\s+")


def human_duration(seconds: float) -> str:
    """把秒数说成人话：不到一分钟说「N 秒」，否则说「N.N 分钟」。"""
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    return f"{seconds / 60:.1f} 分钟"


def sanitize_filename_part(text: str, fallback: str = "") -> str:
    """把任意用户输入清洗成一段安全的文件名（不带扩展名）。

    处理顺序：去首尾空白 → 删控制字符 → 替换非法字符为 ``_`` → 折叠连续空白
    → 去掉首尾的 ``.`` 与空格 → 截断到 32 个字符 → 空则回退 → 命中 Windows
    保留名时前置 ``_``。

    :param text: 原始输入（可能含中文、非法字符、空白）。
    :param fallback: 清洗后为空时返回的兜底值。
    :return: 可安全用作文件名的一段文本。
    """
    if text is None:
        text = ""
    cleaned = str(text)
    cleaned = _CONTROL_CHARS.sub("", cleaned)
    cleaned = _ILLEGAL_FILENAME_CHARS.sub("_", cleaned)
    cleaned = _WHITESPACE_RUN.sub(" ", cleaned)
    cleaned = cleaned.strip(" .")
    if len(cleaned) > MAX_FILENAME_PART_CHARS:
        cleaned = cleaned[:MAX_FILENAME_PART_CHARS]
    cleaned = cleaned.strip(" .")
    if not cleaned:
        return fallback
    if cleaned.upper() in _WINDOWS_RESERVED:
        cleaned = "_" + cleaned
    return cleaned


def default_out_name(prefix: str = "") -> str:
    """默认文件名（不含扩展名）。

    刻意不带风格描述里的词 —— 中文 caption 截出来的文件名在 Windows 上
    容易踩编码和非法字符的坑。用时间戳最稳，想改名加 ``-o`` 就行。

    :param prefix: 可选前缀（网页版「这是给谁做的」）。传入前会做安全清洗。
    :return: ``song-MMDD-HHMM``；有前缀时 ``<前缀>-song-MMDD-HHMM``。
        **无参调用与旧行为逐字一致。**
    """
    stamp = datetime.now().strftime("%m%d-%H%M")
    safe_prefix = sanitize_filename_part(prefix) if prefix else ""
    if safe_prefix:
        return f"{safe_prefix}-song-{stamp}"
    return f"song-{stamp}"


def write_sidecar(
    out_path: Path,
    request: GenerateRequest,
    results: List[Any],
    elapsed: float,
    preset: Optional[StylePreset] = None,
) -> None:
    """把这次的请求和结果落到同名 json，方便之后复现同一首歌。

    :param out_path: 音频输出路径；记录写到同目录同名、后缀换成 ``.json``。
    :param request: 本次提交的请求对象（需有 ``to_payload()``）。
    :param results: ``TaskResult`` 列表。
    :param elapsed: 本次生成耗时（秒）。
    :param preset: 命中的风格模板，可为 ``None``。
    """
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_seconds": round(elapsed, 1),
        "request": request.to_payload(),
        "preset": preset.key if preset else None,
        "results": [
            {
                "file": r.file_url,
                "metas": r.metas,
                "dit_model": r.dit_model,
                "lm_model": r.lm_model,
            }
            for r in results
        ],
        "reproduce": (
            f"sfs make --caption \"{request.prompt}\" "
            f"--lyrics-file <歌词文件> --duration {request.audio_duration:.0f} "
            + (f"--seed {request.seed} " if request.seed is not None else "")
        ).strip(),
    }
    try:
        out_path.with_suffix(".json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        # 记录写不进去不该让生成这件事失败。
        pass
