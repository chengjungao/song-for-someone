# -*- coding: utf-8 -*-
"""风格模板。

ACE-Step 的 ``prompt`` 字段（官方叫 caption）决定「用什么乐器、什么人声、
什么情绪、什么速度」。它同时也是 LM planner 的输入 —— 写得好，planner
补出来的 BPM、调式、曲式结构都会更贴合。

这里内置一批中文场景常见的模板，可以直接用，也可以当写 caption 的范例抄。

**关于"实测"标记**：标 ``实测`` 的模板是作者在 RTX 4080 上真跑过的，
后面的 BPM / 调式是模型当时自己补出来的值。没标的属于按同构句式写出的
合理建议 —— 能用，但别把括号里的数字当承诺，模型每次判的可能不一样。

caption 的写法可以套这个句式：

    乐器 + 人声 + 情绪 + 速度 + 副歌走向

例：``温暖的中文流行民谣，木吉他为主，钢琴点缀，女声，情感真挚，中速，副歌有起伏``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class StylePreset:
    """一个风格模板。"""

    key: str
    name: str
    caption: str
    duration: float = 120.0
    vocal_language: str = "zh"
    verified: bool = False
    note: str = ""
    bpm: Optional[int] = None


PRESETS: Dict[str, StylePreset] = {
    "folk": StylePreset(
        key="folk",
        name="中文民谣 · 木吉他",
        caption="温暖的中文流行民谣，木吉他为主，钢琴点缀，女声，情感真挚，中速，副歌有起伏",
        duration=120.0,
        vocal_language="zh",
        verified=True,
        note="实测曲目：模型自判 79 BPM / G major / 4/4。适合叙事型歌词，句子可以写长一点。",
        bpm=79,
    ),
    "piano": StylePreset(
        key="piano",
        name="钢琴叙事 · 慢板",
        caption="安静的钢琴叙事曲，独奏钢琴为主，弦乐轻铺底，女声，克制而深情，慢速，副歌渐进展开",
        duration=100.0,
        vocal_language="zh",
        verified=True,
        note="实测曲目：模型自判 71 BPM / F major。配短句歌词效果最好，长句会显拖。",
        bpm=71,
    ),
    "warm-pop": StylePreset(
        key="warm-pop",
        name="温暖流行 · 女声",
        caption="温暖的中文流行歌，钢琴与合成器铺底，加入轻鼓点，女声，明亮温柔，中速偏慢，副歌开阔",
        duration=150.0,
        vocal_language="zh",
        note="比民谣更「满」，适合副歌需要明显爆发的歌词。",
    ),
    "birthday": StylePreset(
        key="birthday",
        name="生日祝福 · 轻快",
        caption="轻快的中文生日祝福歌，尤克里里与口琴为主，加入手鼓与掌声，女声，欢快亲切，中快板，副歌朗朗上口",
        duration=90.0,
        vocal_language="zh",
        note="90 秒是生日场景的舒服长度 —— 完整唱一遍刚好，不显冗长。",
    ),
    "child": StylePreset(
        key="child",
        name="童声 · 轻快",
        caption="明亮的中文童声歌曲，钢琴与木琴为主，节奏轻快简单，童声合唱，天真活泼，中速，旋律重复度高",
        duration=90.0,
        vocal_language="zh",
        note="歌词每行控制在 8~12 字，童声唱长句会很赶。",
    ),
    "rock": StylePreset(
        key="rock",
        name="摇滚 · 电吉他",
        caption="充满力量的中文摇滚，失真电吉他为主，贝斯与实鼓打底，男声，粗粝真挚，中快板，副歌嘶吼式爆发",
        duration=150.0,
        vocal_language="zh",
        note="男声 + 高能量，适合情绪浓度高的歌词；温柔词句会被压得听不清。",
    ),
    "lofi": StylePreset(
        key="lofi",
        name="Lo-fi · 纯器乐",
        caption="Lo-fi hip hop 纯器乐，慵懒电钢琴，磁带底噪，柔和鼓刷，无人声，放松，慢速循环",
        duration=120.0,
        vocal_language="zh",
        note="纯器乐请把歌词写成 [Instrumental]，否则模型仍会去唱。",
    ),
    "chinese-traditional": StylePreset(
        key="chinese-traditional",
        name="国风 · 古筝笛子",
        caption="中国风歌曲，古筝与竹笛为主，加入弦乐与轻打点，女声，清雅含蓄，中速，副歌留白有空间感",
        duration=150.0,
        vocal_language="zh",
        note="国风歌词常见四字短句，本工具的行宽检查基本不会触发。",
    ),
    "electronic": StylePreset(
        key="electronic",
        name="电子流行 · 律动",
        caption="中文电子流行，合成器琶音与 pad 铺底，四四拍鼓机，女声，明亮有推进感，中快板，副歌加厚层次",
        duration=150.0,
        vocal_language="zh",
        note="律动强，歌词不宜写得太密 —— 满篇长句会被人声跟不上的鼓点切碎。",
    ),
    "acoustic-male": StylePreset(
        key="acoustic-male",
        name="男声民谣 · 吉他",
        caption="质朴的中文男声民谣，单把木吉他伴奏，口琴间奏，男声，克制低沉，中慢板，副歌情绪略微上扬",
        duration=120.0,
        vocal_language="zh",
        note="单吉他编制，人声清晰 —— 歌词写得好不好，这种编制最容易听出来。",
    ),
}


def list_presets() -> List[StylePreset]:
    """按名称排序列出全部模板。"""
    return sorted(PRESETS.values(), key=lambda p: p.name)


def get_preset(key: str) -> Optional[StylePreset]:
    """按键名取模板。同时接受中文名和模糊匹配。"""
    if not key:
        return None
    lowered = key.strip().lower()
    if lowered in PRESETS:
        return PRESETS[lowered]
    for preset in PRESETS.values():
        if key.strip() == preset.name:
            return preset
    for preset in PRESETS.values():
        if lowered in preset.key or lowered in preset.name.lower():
            return preset
    return None


def format_preset_table() -> str:
    """渲染成终端表格。"""
    lines = ["内置风格模板", "=" * 66]
    for preset in list_presets():
        mark = "实测" if preset.verified else "  建议"
        lines.append(f"  {preset.key:<21} {preset.name:<16} {preset.duration:>5.0f}s  [{mark}]")
    lines.append("")
    lines.append("详细说明用： sfs style <键名>")
    return "\n".join(lines)


def format_preset_detail(preset: StylePreset) -> str:
    """单个模板的详情。"""
    lines = [
        f"{preset.name}（{preset.key}）",
        "=" * 46,
        f"风格描述：{preset.caption}",
        f"建议时长：{preset.duration:.0f} 秒",
        f"人声语言：{preset.vocal_language}",
    ]
    if preset.bpm:
        lines.append(f"参考速度：{preset.bpm} BPM")
    lines.append(f"状态：{'已实测' if preset.verified else '按同构句式给出的建议，未实测'}")
    if preset.note:
        lines.append("")
        lines.append(f"说明：{preset.note}")
    lines.append("")
    lines.append("直接出歌：")
    lines.append(
        f'  sfs make --style {preset.key} --lyrics-file lyrics.txt '
        f'--duration {preset.duration:.0f} --out 我的歌.mp3'
    )
    return "\n".join(lines)
