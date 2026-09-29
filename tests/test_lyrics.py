# -*- coding: utf-8 -*-
"""歌词解析与体检的单元测试。

只用标准库 unittest，不用装任何东西：

    python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from song_for_someone.lyrics import (  # noqa: E402
    MAX_LYRICS_CHARS,
    analyze,
    cjk_ratio,
    display_width,
    format_report,
    normalize_tag,
    parse_sections,
)


class TestDisplayWidth(unittest.TestCase):
    def test_ascii_counts_one(self):
        self.assertEqual(display_width("hello"), 5)

    def test_cjk_counts_two(self):
        self.assertEqual(display_width("生日快乐"), 8)

    def test_mixed(self):
        # "hi" = 2，"，" 是全角 = 2，"你好" = 4
        self.assertEqual(display_width("hi，你好"), 8)

    def test_empty(self):
        self.assertEqual(display_width(""), 0)


class TestNormalizeTag(unittest.TestCase):
    def test_english(self):
        self.assertEqual(normalize_tag("Verse"), "verse")
        self.assertEqual(normalize_tag("CHORUS"), "chorus")

    def test_numbered(self):
        self.assertEqual(normalize_tag("Verse 1"), "verse")
        self.assertEqual(normalize_tag("Chorus 2"), "chorus")

    def test_chinese(self):
        self.assertEqual(normalize_tag("主歌"), "verse")
        self.assertEqual(normalize_tag("副歌"), "chorus")
        self.assertEqual(normalize_tag("纯音乐"), "instrumental")

    def test_pre_chorus_variants(self):
        self.assertEqual(normalize_tag("Pre-Chorus"), "pre_chorus")
        self.assertEqual(normalize_tag("PreChorus"), "pre_chorus")

    def test_unknown(self):
        self.assertEqual(normalize_tag("莫名其妙"), "other")


class TestParseSections(unittest.TestCase):
    def test_basic_split(self):
        lyrics = "\n".join([
            "[Verse 1]",
            "第一行",
            "第二行",
            "",
            "[Chorus]",
            "副歌一行",
        ])
        sections, loose, has_structure = parse_sections(lyrics)
        self.assertTrue(has_structure)
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0].kind, "verse")
        self.assertEqual(sections[0].lines, ["第一行", "第二行"])
        self.assertEqual(sections[1].kind, "chorus")
        self.assertEqual(sections[1].index, 1)

    def test_chinese_brackets(self):
        lyrics = "【主歌】\n一句\n【副歌】\n两句"
        sections, _loose, has_structure = parse_sections(lyrics)
        self.assertTrue(has_structure)
        self.assertEqual([s.kind for s in sections], ["verse", "chorus"])

    def test_no_structure_goes_to_other(self):
        lyrics = "就是一句话\n又一 句话"
        sections, _loose, has_structure = parse_sections(lyrics)
        self.assertFalse(has_structure)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].kind, "other")

    def test_loose_tag_detected_but_not_treated_as_tag(self):
        lyrics = "Verse 1:\n这是一行"
        sections, loose, has_structure = parse_sections(lyrics)
        self.assertTrue(loose)
        self.assertFalse(has_structure)
        # "Verse 1:" 应当留在正文里，而不是被当成结构标签
        self.assertEqual(sections[0].lines[0], "Verse 1:")

    def test_empty_section_dropped(self):
        lyrics = "[Verse 1]\n\n[Chorus]\n只有副歌有词"
        sections, _loose, _has = parse_sections(lyrics)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].kind, "chorus")

    def test_instrumental_kept_even_without_lines(self):
        sections, _loose, _has = parse_sections("[Instrumental]")
        self.assertEqual(len(sections), 1)
        self.assertTrue(sections[0].is_instrumental)


class TestAnalyze(unittest.TestCase):
    def test_clean_lyrics_has_no_issues(self):
        lyrics = "\n".join([
            "[Verse 1]",
            "那年冬天你把围巾留给我",
            "自己缩着脖子走过三条街",
            "我说你傻你笑着说没事",
            "后来才知道你也怕冷",
            "",
            "[Chorus]",
            "生日快乐 我唱得不算好",
            "但我记得你所有的样子",
            "生日快乐 愿你被世界温柔",
            "像你对我那样温柔",
            "",
            "[Verse 2]",
            "你总说记性不好",
            "却记得我不吃香菜",
            "我讲过的每句话",
            "你都当成正经事听",
            "",
            "[Chorus]",
            "生日快乐 我唱得不算好",
            "但我记得你所有的样子",
            "生日快乐 愿你被世界温柔",
            "像你对我那样温柔",
        ])
        report = analyze(lyrics)
        self.assertTrue(report.ok, [i.message for i in report.errors])
        self.assertFalse(report.warnings, [i.message for i in report.warnings])
        self.assertEqual(len(report.sections), 4)

    def test_missing_structure_warns(self):
        report = analyze("这是一段没有任何标签的歌词\n所以模型不知道段落边界")
        codes = [i.code for i in report.issues]
        self.assertIn("no-structure", codes)

    def test_long_line_is_error(self):
        long_line = "啊" * 40  # 80 列，超过 64 列硬阈值
        lyrics = f"[Verse 1]\n{long_line}\n短句"
        report = analyze(lyrics)
        codes = [i.code for i in report.errors]
        self.assertIn("line-too-long", codes)

    def test_medium_line_warns(self):
        medium = "啊" * 24  # 48 列，在 40 与 64 之间
        report = analyze(f"[Verse 1]\n{medium}")
        codes = [i.code for i in report.warnings]
        self.assertIn("line-long", codes)

    def test_over_char_limit_is_error(self):
        lyrics = "[Verse 1]\n" + "啊" * (MAX_LYRICS_CHARS + 100)
        report = analyze(lyrics)
        codes = [i.code for i in report.errors]
        self.assertIn("too-long", codes)

    def test_inconsistent_chorus_warns(self):
        lyrics = "\n".join([
            "[Verse 1]", "主歌内容写一点点",
            "[Chorus]", "第一遍副歌是这样唱的",
            "[Verse 2]", "第二段主歌内容写一点点",
            "[Chorus]", "第二遍副歌完全不一样",
        ])
        report = analyze(lyrics)
        codes = [i.code for i in report.warnings]
        self.assertIn("chorus-mismatch", codes)

    def test_consistent_chorus_does_not_warn(self):
        lyrics = "\n".join([
            "[Verse 1]", "主歌内容写一点点",
            "[Chorus]", "副歌就是这样唱的哦",
            "[Verse 2]", "第二段主歌内容写一点点",
            "[Chorus]", "副歌就是这样唱的哦",
        ])
        report = analyze(lyrics)
        codes = [i.code for i in report.warnings]
        self.assertNotIn("chorus-mismatch", codes)

    def test_instrumental_detected(self):
        report = analyze("[Instrumental]")
        self.assertTrue(report.is_instrumental)
        self.assertLess(report.cjk_ratio, 0.5)

    def test_no_chorus_warns(self):
        lyrics = "[Verse 1]\n只有主歌没有副歌的情况\n再多写一行"
        report = analyze(lyrics)
        codes = [i.code for i in report.warnings]
        self.assertIn("no-chorus", codes)

    def test_section_index_present_on_line_issues(self):
        report = analyze("[Verse 1]\n" + "啊" * 40)
        line_issues = [i for i in report.issues if i.code == "line-too-long"]
        self.assertTrue(line_issues)
        self.assertEqual(line_issues[0].section_index, 0)

    def test_report_renders(self):
        report = analyze("[Verse 1]\n一句歌词写在这里")
        text = format_report(report)
        self.assertIn("歌词体检", text)
        self.assertIn("结构：", text)


class TestCjkRatio(unittest.TestCase):
    def test_pure_chinese(self):
        self.assertGreater(cjk_ratio("今天天气很好"), 0.9)

    def test_pure_english(self):
        self.assertEqual(cjk_ratio("hello world"), 0.0)

    def test_empty(self):
        self.assertEqual(cjk_ratio("   "), 0.0)


if __name__ == "__main__":
    unittest.main()
