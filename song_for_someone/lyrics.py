# -*- coding: utf-8 -*-
"""歌词解析与结构体检。

ACE-Step 的歌词输入是纯文本，靠 ``[Verse]`` / ``[Chorus]`` 这类标签切段落。
上游文档只给了两条硬约束：**总长不超过 4096 字符**、**建议用结构标签**。
其余的坑（一行太长、副歌每遍写得不一样、段落行数忽多忽少）没有地方会提醒你，
但它们在成品里全都听得出来。

这个模块把歌词切成段落，跑一遍体检，把问题分三级报出来：

    ERROR  踩了上游的硬限制，不改就出问题
    WARN   会明显影响成品质量的经验阈值
    HINT   可以更好的建议

体检结论是**仅供参考**的。歌词是创作，阈值是统计出来的常见区间，
不是判卷标准 —— 你的歌你说了算。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# 上游硬限制：歌词最长 4096 字符
MAX_LYRICS_CHARS = 4096

# 经验阈值（本工具判据，非上游规定）
# 以「显示宽度」计：一个汉字算 2 列，一个 ASCII 字符算 1 列。
MAX_LINE_WIDTH_WARN = 40   # 约 20 个汉字。再长容易换不过气。
MAX_LINE_WIDTH_ERROR = 64  # 约 32 个汉字。基本可以确定唱不顺。
SECTION_LINES_WARN = 10    # 单段行数上限的软提醒
MIN_TOTAL_CHARS = 40       # 太短的歌词很难撑起 2 分钟

# 标签 → 归一化类型
TAG_ALIASES: Dict[str, str] = {
    "intro": "intro",
    "前奏": "intro",
    "verse": "verse",
    "主歌": "verse",
    "pre-chorus": "pre_chorus",
    "prechorus": "pre_chorus",
    "pre chorus": "pre_chorus",
    "预副歌": "pre_chorus",
    "chorus": "chorus",
    "副歌": "chorus",
    "hook": "hook",
    "bridge": "bridge",
    "桥段": "bridge",
    "间奏": "interlude",
    "interlude": "interlude",
    "outro": "outro",
    "尾奏": "outro",
    "ending": "outro",
    "instrumental": "instrumental",
    "inst": "instrumental",
    "纯音乐": "instrumental",
    "纯器乐": "instrumental",
    "rap": "rap",
    "说唱": "rap",
    "adlib": "adlib",
    "和声": "adlib",
}

# 归类型 → 中文名，用于报告
KIND_LABELS: Dict[str, str] = {
    "intro": "前奏",
    "verse": "主歌",
    "pre_chorus": "预副歌",
    "chorus": "副歌",
    "hook": "记忆点",
    "bridge": "桥段",
    "interlude": "间奏",
    "outro": "尾奏",
    "instrumental": "纯音乐",
    "rap": "说唱",
    "adlib": "和声",
    "other": "未识别",
}

_TAG_RE = re.compile(r"^\s*[\[【]\s*([^\]】]+?)\s*[\]】]\s*$")

# 段落里"像标签"的写法，用于提示用户可能漏了方括号
_LOOSE_TAG_RE = re.compile(
    r"^\s*(verse|chorus|bridge|intro|outro|pre-?chorus|hook)\s*\d*\s*[:：]\s*$",
    re.IGNORECASE,
)


def display_width(text: str) -> int:
    """按显示宽度计长度：CJK 及全角符号算 2，其余算 1。

    这与公众号排版里算代码块行宽的口径一致 —— 目的是估算"看起来多长"，
    而不是"有几个字符"。
    """
    width = 0
    for char in text:
        code = ord(char)
        if (
            0x1100 <= code <= 0x115F        # 韩文字母
            or 0x2E80 <= code <= 0xA4CF     # CJK 部首 / 假名 / 汉字
            or 0xAC00 <= code <= 0xD7A3     # 韩文音节
            or 0xF900 <= code <= 0xFAFF     # CJK 兼容汉字
            or 0xFE30 <= code <= 0xFE6F     # CJK 兼容形式
            or 0xFF00 <= code <= 0xFF60     # 全角形式
            or 0xFFE0 <= code <= 0xFFE6
            or 0x20000 <= code <= 0x3FFFD   # CJK 扩展
        ):
            width += 2
        else:
            width += 1
    return width


def cjk_ratio(text: str) -> float:
    """汉字（含中文标点）占比。"""
    meaningful = [c for c in text if not c.isspace()]
    if not meaningful:
        return 0.0
    cjk = sum(1 for c in meaningful if _is_cjk(c))
    return cjk / len(meaningful)


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0x3000 <= code <= 0x303F
        or 0xFF00 <= code <= 0xFFEF
        or 0x20000 <= code <= 0x3FFFD
    )


@dataclass
class Section:
    """歌词里的一个段落。"""

    tag: str                 # 原始标签文本，如 "Verse 1"
    kind: str                # 归类型，如 "verse"
    lines: List[str] = field(default_factory=list)
    index: int = 0           # 段落在整首里的序号，从 0 起

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def char_count(self) -> int:
        return len("".join(self.lines))

    @property
    def max_line_width(self) -> int:
        return max((display_width(line) for line in self.lines), default=0)

    @property
    def is_instrumental(self) -> bool:
        return self.kind == "instrumental"


@dataclass
class Issue:
    """一条体检结论。"""

    level: str       # "error" / "warn" / "hint"
    code: str
    message: str
    section_index: Optional[int] = None


@dataclass
class LyricsReport:
    """完整体检报告。"""

    sections: List[Section] = field(default_factory=list)
    issues: List[Issue] = field(default_factory=list)
    total_chars: int = 0
    total_lines: int = 0
    cjk_ratio: float = 0.0
    has_structure: bool = False

    @property
    def errors(self) -> List[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> List[Issue]:
        return [i for i in self.issues if i.level == "warn"]

    @property
    def hints(self) -> List[Issue]:
        return [i for i in self.issues if i.level == "hint"]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def is_instrumental(self) -> bool:
        return bool(self.sections) and all(s.is_instrumental for s in self.sections)

    def chorus_texts(self) -> List[str]:
        """所有副歌段的文本，用于比对"每遍副歌是否一致"。"""
        return [
            re.sub(r"\s+", "", s.text)
            for s in self.sections
            if s.kind == "chorus"
        ]


# ------------------------------------------------------------------ 解析


def parse_sections(lyrics: str) -> Tuple[List[Section], List[str], bool]:
    """把歌词文本切成段落。

    :return: (段落列表, 无标签的行, 是否出现过合法结构标签)
    """
    sections: List[Section] = []
    current: Optional[Section] = None
    loose_lines: List[str] = []
    has_structure = False

    for raw_line in lyrics.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = raw_line.strip()
        if not stripped:
            continue

        tag_match = _TAG_RE.match(stripped)
        if tag_match:
            tag_text = tag_match.group(1).strip()
            current = Section(
                tag=tag_text,
                kind=normalize_tag(tag_text),
                lines=[],
                index=len(sections),
            )
            sections.append(current)
            has_structure = True
            continue

        if _LOOSE_TAG_RE.match(stripped):
            # 形如 "Verse 1:" 但没加方括号 —— 提一句，不当成标签。
            # 注意仍然要把这一行收进段落：解析器丢内容比误判更糟。
            loose_lines.append(stripped)
            if current is None:
                current = Section(
                    tag="(无标签)", kind="other", lines=[], index=len(sections)
                )
                sections.append(current)
            current.lines.append(stripped)
            continue

        if current is None:
            current = Section(tag="(无标签)", kind="other", lines=[], index=len(sections))
            sections.append(current)

        current.lines.append(stripped)

    # 丢掉只写了标签、没写内容的空段
    sections = [s for s in sections if s.lines or s.is_instrumental]
    for position, section in enumerate(sections):
        section.index = position

    return sections, loose_lines, has_structure


def normalize_tag(tag: str) -> str:
    """把 ``Verse 1`` / ``副歌`` 这类标签归一化。"""
    lowered = tag.strip().lower()
    # 去掉尾部编号：Verse 1 → verse；Chorus 2 → chorus
    for alias in sorted(TAG_ALIASES, key=len, reverse=True):
        if lowered == alias or lowered.startswith(alias + " ") or lowered.startswith(alias + "1"):
            return TAG_ALIASES[alias]
    # 纯数字或无法识别的标签
    if re.fullmatch(r"[0-9]+", lowered):
        return "other"
    for alias in sorted(TAG_ALIASES, key=len, reverse=True):
        if alias in lowered:
            return TAG_ALIASES[alias]
    return "other"


# ------------------------------------------------------------------ 体检


def analyze(lyrics: str) -> LyricsReport:
    """跑一遍歌词体检。"""
    sections, loose_lines, has_structure = parse_sections(lyrics)
    total_chars = len(lyrics.replace("\n", "").replace("\r", ""))
    total_lines = len([ln for ln in lyrics.splitlines() if ln.strip()])

    report = LyricsReport(
        sections=sections,
        total_chars=total_chars,
        total_lines=total_lines,
        cjk_ratio=cjk_ratio(lyrics),
        has_structure=has_structure,
    )

    issues = report.issues

    # --- 硬限制 ---
    if total_chars > MAX_LYRICS_CHARS:
        issues.append(Issue(
            "error", "too-long",
            f"歌词 {total_chars} 字符，超过上游上限 {MAX_LYRICS_CHARS}。"
            f"需要删掉约 {total_chars - MAX_LYRICS_CHARS} 个字符。",
        ))

    if total_chars < MIN_TOTAL_CHARS:
        issues.append(Issue(
            "warn", "too-short",
            f"歌词只有 {total_chars} 字符，可能撑不起一首完整歌曲。"
            "短歌词建议把时长也调短，或改为纯音乐（[Instrumental]）。",
        ))

    # --- 结构 ---
    if not has_structure:
        issues.append(Issue(
            "warn", "no-structure",
            "没有检测到 [Verse] / [Chorus] 这类结构标签。"
            "加上标签能让模型按段落的情绪起伏来唱，成品结构会明显更清楚。",
        ))

    if loose_lines:
        sample = "、".join(loose_lines[:3])
        issues.append(Issue(
            "hint", "loose-tag",
            f"这些行看起来想当标签写，但没加方括号：{sample}。"
            "标签要写成 [Verse 1] 这种带方括号的形式才会被识别。",
        ))

    if report.is_instrumental:
        issues.append(Issue(
            "hint", "instrumental",
            "这是一首纯音乐，不会有任何人声。想让人声唱起来就把 [Instrumental] 换成真实歌词。",
        ))
    elif sections and not any(s.kind == "chorus" for s in sections):
        issues.append(Issue(
            "warn", "no-chorus",
            "没有副歌段（[Chorus]）。副歌是整首歌被记住的那一句，"
            "没有它成品容易听起来像一段长的前奏。",
        ))

    # --- 逐段检查 ---
    for section in sections:
        if section.is_instrumental:
            continue

        for line_no, line in enumerate(section.lines, start=1):
            width = display_width(line)
            if width > MAX_LINE_WIDTH_ERROR:
                issues.append(Issue(
                    "error", "line-too-long",
                    f"第 {line_no} 行宽度 {width} 列（约 {width // 2} 个汉字），"
                    f"超过 {MAX_LINE_WIDTH_ERROR} 列的硬阈值。演唱时会挤成一团，建议拆成两行。",
                    section.index,
                ))
            elif width > MAX_LINE_WIDTH_WARN:
                issues.append(Issue(
                    "warn", "line-long",
                    f"第 {line_no} 行宽度 {width} 列（约 {width // 2} 个汉字），"
                    f"偏长（经验阈值 {MAX_LINE_WIDTH_WARN} 列）。唱起来可能换不过气。",
                    section.index,
                ))

        if len(section.lines) > SECTION_LINES_WARN:
            issues.append(Issue(
                "hint", "section-long",
                f"{section.label}有 {len(section.lines)} 行，比常见的 4~8 行长不少。"
                "段落太长时模型容易把情绪铺平。",
                section.index,
            ))

        if section.char_count < 6 and not section.is_instrumental:
            issues.append(Issue(
                "hint", "section-thin",
                f"{section.label}只有 {section.char_count} 个字符，信息量偏少，"
                "在成品里可能被一带而过。",
                section.index,
            ))

    # --- 副歌一致性 ---
    chorus_texts = report.chorus_texts()
    if len(chorus_texts) > 1:
        unique = set(chorus_texts)
        if len(unique) > 1:
            issues.append(Issue(
                "warn", "chorus-mismatch",
                f"有 {len(chorus_texts)} 段副歌，但内容各不相同。"
                "副歌重复是让人记住这首歌的主要手段，"
                "如果不是刻意做「副歌递进」，建议把各遍副歌唱词统一。",
            ))

    # --- 语言 ---
    if report.cjk_ratio < 0.3 and any(
        s.kind in ("verse", "chorus") for s in sections
    ):
        issues.append(Issue(
            "hint", "mostly-non-cjk",
            f"汉字占比约 {report.cjk_ratio:.0%}，看起来以非中文为主。"
            "如果确实要唱中文，记得把 vocal_language 设为 zh；"
            "如果是英文歌，设成 en 更合适。",
        ))

    return report


# ------------------------------------------------------------------ 输出


def format_report(report: LyricsReport, show_sections: bool = True) -> str:
    """把体检报告渲染成终端可读的文本。"""
    lines: List[str] = []
    lines.append("歌词体检")
    lines.append("=" * 46)
    lines.append(
        f"段落 {len(report.sections)} ｜ 行数 {report.total_lines} ｜ "
        f"字符 {report.total_chars}/{MAX_LYRICS_CHARS} ｜ 汉字占比 {report.cjk_ratio:.0%}"
    )

    if show_sections and report.sections:
        lines.append("")
        lines.append("结构：")
        for section in report.sections:
            mark = "♪" if section.is_instrumental else "·"
            lines.append(
                f"  {mark} [{section.index}] {section.tag:<12} "
                f"{section.label:<5} {len(section.lines):>2} 行  "
                f"最宽 {section.max_line_width:>2} 列"
            )

    if report.issues:
        lines.append("")
        lines.append("问题：")
        for level, title in (("error", "错误"), ("warn", "警告"), ("hint", "建议")):
            bucket = [i for i in report.issues if i.level == level]
            if not bucket:
                continue
            for issue in bucket:
                where = ""
                if issue.section_index is not None:
                    where = f"（第 {issue.section_index + 1} 段）"
                lines.append(f"  [{title}] {issue.message}{where}")
    else:
        lines.append("")
        lines.append("没有发现问题。可以出歌了。")

    lines.append("")
    if report.ok:
        lines.append("结论：可以提交。")
    else:
        lines.append("结论：有错误项，建议改完再提交。")
    return "\n".join(lines)


def build_prompt_hint(report: LyricsReport) -> str:
    """根据体检结果给一句风格描述方向的提示（不是自动写 caption）。

    只做「该往哪个方向补」的提示，不替用户决定具体措辞。
    """
    tips: List[str] = []
    if not report.has_structure:
        tips.append("先补结构标签")
    if any(s.kind == "chorus" for s in report.sections):
        tips.append("副歌情绪要能被描述出来")
    if report.cjk_ratio > 0.6:
        tips.append("风格描述建议写成中文，人声指定 zh")
    if not tips:
        return ""
    return "；".join(tips) + "。"
