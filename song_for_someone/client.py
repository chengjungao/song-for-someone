# -*- coding: utf-8 -*-
"""ACE-Step 1.5 HTTP API 客户端。

只用 Python 标准库，不引入任何第三方依赖。

上游（ACE-Step 1.5）暴露的端点是裸 HTTP 接口，没有提供 Python 客户端，
调用方得自己拼 JSON、自己轮询、自己处理相对路径和失败重试。这个模块把
那部分收进来，让「写歌」这件事变成三行代码：

    client = AceStepClient()
    task_id = client.submit(GenerateRequest(prompt="...", lyrics="..."))
    results = client.wait(task_id)

端点约定（对应上游 acestep/api_server.py）：

    GET  /health         健康检查
    POST /release_task   提交生成任务，返回 task_id
    POST /query_result   按 task_id 查询结果

任务状态位：0 = 排队/生成中，1 = 成功，2 = 失败。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import net

DEFAULT_BASE_URL = "http://127.0.0.1:8001"
DEFAULT_TIMEOUT = 60.0

# 任务状态位
STATUS_RUNNING = 0
STATUS_SUCCESS = 1
STATUS_FAILED = 2

STATUS_TEXT = {
    STATUS_RUNNING: "生成中",
    STATUS_SUCCESS: "已完成",
    STATUS_FAILED: "失败",
}


class AceStepError(RuntimeError):
    """与 ACE-Step 服务交互时的通用错误。"""


class ServiceUnreachable(AceStepError):
    """服务不可达 —— 多半是没启动，或端口/地址填错了。"""


@dataclass
class GenerateRequest:
    """一次生成请求。

    字段名刻意与上游 /release_task 的参数名保持一致，方便对照官方文档。
    只暴露常用的那些；低频参数直接走 ``extra`` 透传。
    """

    # 风格描述（英文叫 caption）。写「什么乐器、什么人声、什么情绪、什么速度」。
    prompt: str = ""

    # 歌词。分节用 [Verse] / [Chorus] / [Bridge] 标记；纯音乐写 [Instrumental]。
    lyrics: str = "[Instrumental]"

    # 让模型先思考一遍再唱（走 LM planner）。关掉更快，但质量会掉。
    thinking: bool = True

    # 人声语言。中文写 "zh"。
    vocal_language: str = "zh"

    # 音频时长（秒）。上游支持 10 ~ 600。
    audio_duration: float = 120.0

    # 扩散步数。8 是官方 turbo 模型的推荐值，调高更慢、未必更好。
    inference_steps: int = 8

    # 一次出几版。想多听几个版本再挑，就调大。
    batch_size: int = 1

    # mp3 / wav / flac
    audio_format: str = "mp3"

    # 固定种子可以复现同一首歌。None 表示随机。
    seed: Optional[int] = None

    # 下面这些留空则由模型自己判断，本机会把判断结果写在返回的 metas 里。
    bpm: Optional[int] = None
    key_scale: str = ""
    time_signature: str = ""

    guidance_scale: float = 7.0

    # text2music / cover / repaint / lego / extract / complete
    task_type: str = "text2music"

    # 低频参数透传口子，键名直接对应上游字段。
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> Dict[str, Any]:
        """转成 /release_task 的请求体。"""
        payload: Dict[str, Any] = {
            "prompt": self.prompt,
            "lyrics": self.lyrics,
            "thinking": bool(self.thinking),
            "vocal_language": self.vocal_language,
            "audio_duration": float(self.audio_duration),
            "inference_steps": int(self.inference_steps),
            "batch_size": int(self.batch_size),
            "audio_format": self.audio_format,
            "guidance_scale": float(self.guidance_scale),
            "task_type": self.task_type,
            "use_random_seed": self.seed is None,
        }
        if self.seed is not None:
            payload["seed"] = int(self.seed)
        if self.bpm is not None:
            payload["bpm"] = int(self.bpm)
        if self.key_scale:
            payload["key_scale"] = self.key_scale
        if self.time_signature:
            payload["time_signature"] = self.time_signature
        payload.update(self.extra)
        return payload


@dataclass
class TaskResult:
    """一首生成好的歌。"""

    file_url: str
    metas: Dict[str, Any] = field(default_factory=dict)
    dit_model: str = ""
    lm_model: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def bpm(self) -> Optional[int]:
        value = first_meta(self.metas, "bpm")
        return int(value) if isinstance(value, (int, float)) else None

    @property
    def key_scale(self) -> str:
        # 上游 metas 里这个字段叫 keyscale（没有下划线），
        # 但不同版本/不同管线可能给 key_scale，两个都认。
        return str(first_meta(self.metas, "keyscale", "key_scale") or "")

    @property
    def time_signature(self) -> str:
        return str(first_meta(self.metas, "timesignature", "time_signature") or "")

    @property
    def duration(self) -> Optional[float]:
        value = first_meta(self.metas, "duration", "audio_duration", "actual_duration")
        return float(value) if isinstance(value, (int, float)) else None

    def describe(self) -> str:
        """一行摘要，给终端用。"""
        bits = []
        if self.bpm:
            bits.append(f"{self.bpm} BPM")
        if self.key_scale:
            bits.append(self.key_scale)
        if self.duration:
            bits.append(f"{self.duration:.0f} 秒")
        if self.dit_model:
            bits.append(self.dit_model)
        return " ｜ ".join(bits) if bits else "(无元数据)"


def first_meta(metas: Dict[str, Any], *names: str) -> Any:
    """按顺序取第一个有意义的元数据值。

    上游会把没判出来的字段填成空串或字符串 "N/A"，这些都要当"没有"看。
    """
    for name in names:
        value = metas.get(name)
        if value is None:
            continue
        if isinstance(value, str) and value.strip() in ("", "N/A", "n/a", "none"):
            continue
        return value
    return None


class AceStepClient:
    """ACE-Step 本地服务的瘦客户端。

    :param base_url: 服务地址，默认 ``http://127.0.0.1:8001``
    :param timeout: 单次 HTTP 请求超时（秒）。生成任务本身是异步的，
        这里只影响提交和查询这两个短请求。
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ---------------------------------------------------------------- 底层

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = self.base_url + path
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with net.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            raise AceStepError(f"HTTP {exc.code} {url}\n{detail}") from exc
        except urllib.error.URLError as exc:
            raise ServiceUnreachable(
                f"连不上 {self.base_url}（{exc.reason}）。"
                "请确认 ACE-Step 的 API 服务已启动。"
            ) from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise AceStepError(f"响应不是合法 JSON：{body[:200]}") from exc

    def _get(self, path: str) -> Dict[str, Any]:
        url = self.base_url + path
        try:
            with net.urlopen(url, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise ServiceUnreachable(
                f"连不上 {self.base_url}（{exc.reason}）。"
            ) from exc

    # ---------------------------------------------------------------- 接口

    def health(self) -> bool:
        """服务是否活着。不抛异常，失败返回 False。"""
        try:
            self._get("/health")
            return True
        except Exception:
            return False

    def submit(self, request: GenerateRequest) -> str:
        """提交生成任务，返回 task_id。"""
        resp = self._post("/release_task", request.to_payload())
        task_id = (resp.get("data") or {}).get("task_id")
        if not task_id:
            raise AceStepError(
                "提交成功但没拿到 task_id，原始响应：" + json.dumps(resp, ensure_ascii=False)[:400]
            )
        return str(task_id)

    def query(self, task_ids: List[str]) -> List[Dict[str, Any]]:
        """批量查询任务状态。"""
        resp = self._post("/query_result", {"task_id_list": list(task_ids)})
        return resp.get("data") or []

    def wait(
        self,
        task_id: str,
        timeout: float = 1800.0,
        interval: float = 5.0,
        on_progress: Optional[Callable[[float, int], None]] = None,
    ) -> List[TaskResult]:
        """阻塞等待任务完成，返回生成结果列表。

        :param timeout: 总超时（秒）
        :param interval: 轮询间隔（秒）
        :param on_progress: 每次轮询回调 ``(已耗时秒, 状态码)``
        :raises AceStepError: 任务失败或超时
        """
        started = time.time()
        deadline = started + timeout

        while time.time() < deadline:
            time.sleep(interval)
            elapsed = time.time() - started

            try:
                items = self.query([task_id])
            except AceStepError:
                # 单次查询抖动不该让整个任务失败，下一轮再试。
                if on_progress:
                    on_progress(elapsed, STATUS_RUNNING)
                continue

            if not items:
                if on_progress:
                    on_progress(elapsed, STATUS_RUNNING)
                continue

            item = items[0]
            status = item.get("status")

            if status == STATUS_FAILED:
                raise AceStepError(
                    "生成失败：" + json.dumps(item, ensure_ascii=False)[:600]
                )

            if status == STATUS_SUCCESS:
                return self._parse_results(item)

            if on_progress:
                on_progress(elapsed, STATUS_RUNNING if status is None else int(status))

        raise AceStepError(f"等待超时（{timeout:.0f} 秒），task_id={task_id}")

    @staticmethod
    def _parse_results(item: Dict[str, Any]) -> List[TaskResult]:
        raw_result = item.get("result")
        if isinstance(raw_result, str):
            try:
                raw_result = json.loads(raw_result)
            except json.JSONDecodeError:
                raw_result = []
        if not isinstance(raw_result, list):
            raw_result = []

        results: List[TaskResult] = []
        for entry in raw_result:
            if not isinstance(entry, dict):
                continue
            results.append(
                TaskResult(
                    file_url=str(entry.get("file") or ""),
                    metas=entry.get("metas") or {},
                    dit_model=str(entry.get("dit_model") or ""),
                    lm_model=str(entry.get("lm_model") or ""),
                    raw=entry,
                )
            )
        return results

    def absolute_url(self, url: str) -> str:
        """把服务返回的相对路径补成完整 URL。"""
        if not url:
            return ""
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if url.startswith("/"):
            return self.base_url + url
        return self.base_url + "/" + url

    def download(self, url: str, out_path: Path) -> Path:
        """把生成结果下载到本地。"""
        full = self.absolute_url(url)
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            net.urlretrieve(full, str(out_path))
        except urllib.error.URLError as exc:
            raise AceStepError(f"下载失败 {full}：{exc}") from exc
        return out_path

    def generate(
        self,
        request: GenerateRequest,
        out_path: Path,
        timeout: float = 1800.0,
        interval: float = 5.0,
        on_progress: Optional[Callable[[float, int], None]] = None,
    ) -> List[TaskResult]:
        """提交 → 等待 → 下载，一步到位。"""
        task_id = self.submit(request)
        results = self.wait(task_id, timeout=timeout, interval=interval, on_progress=on_progress)

        saved: List[TaskResult] = []
        out_path = Path(out_path)
        for index, result in enumerate(results):
            if len(results) == 1:
                target = out_path
            else:
                target = out_path.with_name(
                    f"{out_path.stem}_{index}{out_path.suffix or '.mp3'}"
                )
            self.download(result.file_url, target)
            saved.append(result)
        return saved


def request_to_dict(request: GenerateRequest) -> Dict[str, Any]:
    """调试用：把请求摊平成普通 dict。"""
    return asdict(request)


def quote_path(path: str) -> str:
    """给本机文件路径做 URL 编码，用于 cover/repaint 这类要传参考音频的任务。"""
    return urllib.parse.quote(str(path))
