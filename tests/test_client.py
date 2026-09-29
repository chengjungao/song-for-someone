# -*- coding: utf-8 -*-
"""API 客户端的单元测试（不联网，只测请求构造与 URL 处理）。"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from song_for_someone.client import (  # noqa: E402
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    AceStepClient,
    GenerateRequest,
    TaskResult,
    first_meta,
)


class TestGenerateRequest(unittest.TestCase):
    def test_defaults(self):
        payload = GenerateRequest(prompt="温暖民谣", lyrics="[Instrumental]").to_payload()
        self.assertEqual(payload["prompt"], "温暖民谣")
        self.assertEqual(payload["lyrics"], "[Instrumental]")
        self.assertEqual(payload["vocal_language"], "zh")
        self.assertEqual(payload["audio_duration"], 120.0)
        self.assertEqual(payload["inference_steps"], 8)
        self.assertTrue(payload["thinking"])

    def test_random_seed_flag_when_seed_absent(self):
        payload = GenerateRequest().to_payload()
        self.assertTrue(payload["use_random_seed"])
        self.assertNotIn("seed", payload)

    def test_seed_included_when_given(self):
        payload = GenerateRequest(seed=42).to_payload()
        self.assertFalse(payload["use_random_seed"])
        self.assertEqual(payload["seed"], 42)

    def test_optional_fields_omitted_when_empty(self):
        payload = GenerateRequest().to_payload()
        self.assertNotIn("bpm", payload)
        self.assertNotIn("key_scale", payload)
        self.assertNotIn("time_signature", payload)

    def test_optional_fields_included_when_given(self):
        payload = GenerateRequest(bpm=79, key_scale="G major", time_signature="4/4").to_payload()
        self.assertEqual(payload["bpm"], 79)
        self.assertEqual(payload["key_scale"], "G major")
        self.assertEqual(payload["time_signature"], "4/4")

    def test_extra_passthrough(self):
        payload = GenerateRequest(extra={"lm_backend": "vllm"}).to_payload()
        self.assertEqual(payload["lm_backend"], "vllm")

    def test_duration_is_float(self):
        payload = GenerateRequest(audio_duration=90).to_payload()
        self.assertIsInstance(payload["audio_duration"], float)
        self.assertEqual(payload["audio_duration"], 90.0)


class TestAbsoluteUrl(unittest.TestCase):
    def setUp(self):
        self.client = AceStepClient("http://127.0.0.1:8001")

    def test_absolute_passthrough(self):
        url = "https://example.com/a.mp3"
        self.assertEqual(self.client.absolute_url(url), url)

    def test_leading_slash(self):
        self.assertEqual(
            self.client.absolute_url("/v1/audio?path=x"),
            "http://127.0.0.1:8001/v1/audio?path=x",
        )

    def test_bare_relative(self):
        self.assertEqual(self.client.absolute_url("v1/audio"), "http://127.0.0.1:8001/v1/audio")

    def test_empty(self):
        self.assertEqual(self.client.absolute_url(""), "")


class TestBaseUrlNormalization(unittest.TestCase):
    def test_trailing_slash_stripped(self):
        client = AceStepClient("http://127.0.0.1:8001/")
        self.assertEqual(client.base_url, "http://127.0.0.1:8001")

    def test_custom_port(self):
        client = AceStepClient("http://192.168.1.10:9000")
        self.assertEqual(client.base_url, "http://192.168.1.10:9000")


class TestFirstMeta(unittest.TestCase):
    def test_returns_first_meaningful(self):
        self.assertEqual(first_meta({"a": 1, "b": 2}, "a", "b"), 1)

    def test_skips_missing(self):
        self.assertEqual(first_meta({"b": 2}, "a", "b"), 2)

    def test_skips_na(self):
        self.assertEqual(first_meta({"a": "N/A", "b": 2}, "a", "b"), 2)

    def test_skips_empty_string(self):
        self.assertEqual(first_meta({"a": "", "b": 2}, "a", "b"), 2)

    def test_returns_none_when_all_missing(self):
        self.assertIsNone(first_meta({}, "a", "b"))


class TestTaskResultMetas(unittest.TestCase):
    """上游 metas 的字段名和文档并不完全一致（实际返回 keyscale 而不是
    key_scale），而且没判出来的字段会填成 "N/A"。这组用例把兼容逻辑钉住，
    防止以后改坏。
    """

    def test_keyscale_without_underscore(self):
        result = TaskResult(file_url="x", metas={"keyscale": "G major"})
        self.assertEqual(result.key_scale, "G major")

    def test_keyscale_with_underscore(self):
        result = TaskResult(file_url="x", metas={"key_scale": "D major"})
        self.assertEqual(result.key_scale, "D major")

    def test_missing_key_scale(self):
        result = TaskResult(file_url="x", metas={})
        self.assertEqual(result.key_scale, "")

    def test_bpm(self):
        result = TaskResult(file_url="x", metas={"bpm": 77})
        self.assertEqual(result.bpm, 77)

    def test_bpm_na_is_none(self):
        result = TaskResult(file_url="x", metas={"bpm": "N/A"})
        self.assertIsNone(result.bpm)

    def test_duration(self):
        result = TaskResult(file_url="x", metas={"duration": 120.0})
        self.assertEqual(result.duration, 120.0)

    def test_time_signature(self):
        result = TaskResult(file_url="x", metas={"timesignature": "4"})
        self.assertEqual(result.time_signature, "4")

    def test_describe_includes_key_and_bpm(self):
        result = TaskResult(
            file_url="x",
            metas={"bpm": 77, "keyscale": "D major", "duration": 120},
        )
        text = result.describe()
        self.assertIn("77 BPM", text)
        self.assertIn("D major", text)

    def test_describe_empty(self):
        result = TaskResult(file_url="x", metas={})
        self.assertEqual(result.describe(), "(无元数据)")


class TestStatusConstants(unittest.TestCase):
    def test_status_values_match_upstream(self):
        # 这三个值是上游 api_server 的约定，改动会静默破坏轮询逻辑，
        # 所以钉死在这里。
        self.assertEqual(STATUS_RUNNING, 0)
        self.assertEqual(STATUS_SUCCESS, 1)
        self.assertEqual(STATUS_FAILED, 2)


if __name__ == "__main__":
    unittest.main()
