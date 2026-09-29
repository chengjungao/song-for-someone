# -*- coding: utf-8 -*-
"""本地网页服务（纯 Python 标准库，零第三方依赖）。

把命令行里那几件事搬到浏览器：环境自检、歌词实时体检、风格下拉、
出歌（诚实进度条）、试听下载、我的作品。

设计要点（与 ``docs/design/ARCH-web-ui.md`` 一致）：

* HTTP 服务用 ``http.server.ThreadingHTTPServer``：每个连接一个线程，
  下载音频、轮询进度、拉静态资源互不阻塞。
* 长耗时的生成跑在 **daemon 工作线程** 里，请求只做「校验 + 抢锁 + 建记录 +
  起线程」，立即返回 ``202``。这样即使浏览器关掉，任务照样跑完。
* 进程内 **全局单任务锁**（``TaskStore._generation_lock``）：第二个请求
  明确返回 ``409 busy``，不静默排队（16GB 显存跑不了两个生成）。
* 上游只有三态轮询（0/1/2）、最快 5 秒一次。服务端**只透出「已耗时」**
  （每次都现算 ``now - started_at``），由前端本地插值驱动进度条。

依赖铁律：本模块只 import 标准库 + 包内的 ``client`` / ``lyrics`` /
``doctor`` / ``styles`` / ``common``，**绝不 import ``cli``**。
"""

from __future__ import annotations

import argparse
import functools
import json
import mimetypes
import os
import socket
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import __version__
from . import net
from .client import (
    DEFAULT_BASE_URL,
    AceStepClient,
    AceStepError,
    GenerateRequest,
    ServiceUnreachable,
    first_meta,
)
from .common import DEFAULT_OUT_DIR, default_out_name, human_duration, write_sidecar
from .doctor import LEVEL_LABELS, DoctorReport, run_doctor
from .lyrics import MAX_LYRICS_CHARS, LyricsReport, analyze as analyze_lyrics
from .styles import StylePreset, get_preset, list_presets

# ------------------------------------------------------------------ 常量

HOST = "127.0.0.1"                 # 只监听本机（红线：禁止 0.0.0.0）
DEFAULT_PORT = 8770
PORT_RANGE = 30                    # 8770..8799 顺延试探

REPO_ROOT = Path(__file__).resolve().parent.parent
WEBAPP_DIR = Path(__file__).resolve().parent / "webapp"
SONGS_DIR = REPO_ROOT / DEFAULT_OUT_DIR          # 固定指向仓库根的 songs/
EXAMPLES_DIR = REPO_ROOT / "examples" / "lyrics"

# 进度预估（经验值，集中在这里便于调参）
BASE_SECONDS = 15.0                # 预热后 2 分钟的歌约 15 秒（README 实测）
COLD_MULTIPLIER = 2.0              # 本次会话首首 ×2（覆盖模型加载）
MIN_ESTIMATE = 8.0

DEFAULT_TIMEOUT = 1800.0           # 生成总超时（秒），与 CLI 一致
DEFAULT_INTERVAL = 5.0             # 轮询间隔（秒），与 CLI 一致
HTTP_TIMEOUT = 60.0                # 单次 HTTP 超时（秒）
HEALTH_TIMEOUT = 2.0               # /api/env 里的上游快查超时

MAX_TASKS = 20                     # 内存里最多保留多少条任务记录
BUSY_MESSAGE = "正在生成中，等这一首出完再点。"

# 时长候选：覆盖全部内置模板的实际 duration 值（来自 styles.py）
DURATION_OPTIONS = [60, 90, 100, 120, 150, 180]

AUDIO_FORMATS = ("mp3", "wav", "flac")

# 载入示例的清单（界面按钮用）
EXAMPLES = [
    {"key": "birthday", "name": "生日祝福"},
    {"key": "lullaby", "name": "摇篮曲"},
]

STATIC_ROUTES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/style.css": "style.css",
    "/favicon.svg": "favicon.svg",
}

# 界面用的「友好键名」。前端不出现 caption/inference_steps/... 这些裸术语。
_ADVANCED_ALIASES = {
    "fixed_id": "seed",
    "tempo": "bpm",
    "tune": "key_scale",
    "steps": "inference_steps",
    "versions": "batch_size",
    "file_type": "audio_format",
    "draft_first": "thinking",
}

_FALLBACK_HTML = (
    "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
    "<title>song-for-someone</title></head><body style=\"font-family:sans-serif;"
    "max-width:640px;margin:60px auto;line-height:1.7\"><h1>界面资源缺失</h1>"
    "<p>服务本身是活的，但没找到 <code>song_for_someone/webapp/</code> 目录下的"
    "静态文件。请确认这个项目是完整复制/下载下来的。</p>"
    "<p>存活探测：<a href=\"/health\">/health</a>　接口列表：<a href=\"/api/meta\">"
    "/api/meta</a></p></body></html>"
)

# ------------------------------------------------------------------ 可注入点
# 测试会 monkeypatch 这几个模块级变量，换掉真实客户端 / 自检 / 目录。


def _default_client_factory(base_url: str, timeout: float) -> AceStepClient:
    """生产用的客户端工厂。测试可把它换成返回假客户端的函数。"""
    return AceStepClient(base_url, timeout=timeout)


_client_factory: Callable[[str, float], Any] = _default_client_factory
_doctor_runner: Callable[..., DoctorReport] = run_doctor
_base_url: str = DEFAULT_BASE_URL


# ------------------------------------------------------------------ 任务状态


@dataclass
class TaskRecord:
    """一次生成任务的内存记录。"""

    task_id: str
    status: str = "queued"             # queued|running|done|failed|timeout|cancelled
    phase: str = "queued"              # queued|submitting|running|finalizing|done|failed
    created_at: float = 0.0
    started_at: float = 0.0            # 提交那一刻
    finished_at: float = 0.0
    estimated_total: float = 15.0
    out_name: str = ""
    out_path: Optional[Path] = None
    request_obj: Optional[GenerateRequest] = None
    request_payload: Dict[str, Any] = field(default_factory=dict)
    preset: Optional[StylePreset] = None
    upstream_task_id: Optional[str] = None
    language: str = "zh"
    language_label: str = "将用中文演唱"
    result: Optional[dict] = None
    error: Optional[dict] = None
    cancel_flag: threading.Event = field(default_factory=threading.Event)

    def elapsed(self) -> float:
        """已耗时（秒）。服务端**每次现算**，绝不缓存 —— 前端插值靠它。"""
        if self.status in ("done", "failed", "timeout", "cancelled") and self.finished_at:
            return max(0.0, self.finished_at - self.started_at)
        return max(0.0, time.time() - self.started_at)

    def to_public(self) -> dict:
        """给前端的视图（不含 request_obj 等内部对象）。"""
        return {
            "task_id": self.task_id,
            "status": self.status,
            "phase": self.phase,
            "started_at": self.started_at,
            "elapsed": round(self.elapsed(), 1),
            "estimated_total": round(self.estimated_total, 1),
            "upstream_task_id": self.upstream_task_id,
            "out_name": self.out_name,
            "language": self.language,
            "language_label": self.language_label,
            "result": self.result,
            "error": self.error,
        }


class TaskStore:
    """内存任务表 + 全局单任务锁。

    多线程下自加锁：``_tasks_lock`` 保护任务表，``_generation_lock`` 是
    进程级的「同时只跑一个生成」闸门。
    """

    def __init__(self) -> None:
        self._tasks: Dict[str, TaskRecord] = {}
        self._order: List[str] = []
        self._tasks_lock = threading.Lock()
        self._generation_lock = threading.Lock()
        self.session_has_success = False

    def create(self, record: TaskRecord) -> TaskRecord:
        """登记一条记录，并按上限裁剪最老的记录。"""
        with self._tasks_lock:
            self._tasks[record.task_id] = record
            self._order.append(record.task_id)
            while len(self._order) > MAX_TASKS:
                old = self._order.pop(0)
                self._tasks.pop(old, None)
        return record

    def get(self, task_id: str) -> Optional[TaskRecord]:
        """按 id 取记录，没有则 None。"""
        with self._tasks_lock:
            return self._tasks.get(task_id)

    def current(self) -> Optional[TaskRecord]:
        """当前「排队中或生成中」的记录（最新一条），没有则 None。"""
        with self._tasks_lock:
            for task_id in reversed(self._order):
                record = self._tasks.get(task_id)
                if record is not None and record.status in ("queued", "running"):
                    return record
        return None

    def try_acquire_generation(self) -> bool:
        """非阻塞抢锁。抢到 True，被别人占着 False。"""
        return self._generation_lock.acquire(blocking=False)

    def release_generation(self) -> None:
        """释放生成锁（已释放则忽略）。"""
        try:
            self._generation_lock.release()
        except RuntimeError:
            pass

    def mark_success(self) -> None:
        """记下「本进程已成功出过至少一首」，用于冷启动预估。"""
        self.session_has_success = True

    def reset(self) -> None:
        """清空所有状态（供测试用）。"""
        with self._tasks_lock:
            self._tasks.clear()
            self._order.clear()
        self.session_has_success = False
        self._generation_lock = threading.Lock()


store = TaskStore()


def estimate_seconds(duration: float, batch_size: int, session_has_success: bool) -> float:
    """估算出歌耗时（秒）。前端只用它当进度条分母。"""
    base = BASE_SECONDS * (float(duration) / 120.0) * max(1, int(batch_size))
    if not session_has_success:
        base *= COLD_MULTIPLIER
    return max(MIN_ESTIMATE, base)


# ------------------------------------------------------------------ 语言判断


def decide_language(report: LyricsReport) -> str:
    """按歌词自动判断演唱语言（CEO A5 决策）。

    汉字占比 > 0.3 用 ``zh``，否则用 ``en``；纯器乐用 ``zh``（无影响）。
    """
    if report.is_instrumental:
        return "zh"
    return "zh" if report.cjk_ratio > 0.3 else "en"


def language_label(language: str) -> str:
    """把语言码说成一句大白话。界面不出现 vocal_language 这个词。"""
    return "将用英文演唱" if language == "en" else "将用中文演唱"


# ------------------------------------------------------------------ 序列化


def serialize_lyrics(report: LyricsReport) -> dict:
    """``LyricsReport`` → 界面用的 dict（比 ``cmd_check --json`` 多几个展示字段）。"""
    return {
        "total_chars": report.total_chars,
        "total_lines": report.total_lines,
        "cjk_ratio": round(report.cjk_ratio, 3),
        "has_structure": report.has_structure,
        "ok": report.ok,
        "is_instrumental": report.is_instrumental,
        "max_chars": MAX_LYRICS_CHARS,
        "sections": [
            {
                "index": s.index,
                "tag": s.tag,
                "kind": s.kind,
                "label": s.label,
                "lines": len(s.lines),
                "max_line_width": s.max_line_width,
                "is_instrumental": s.is_instrumental,
            }
            for s in report.sections
        ],
        "issues": [
            {
                "level": i.level,
                "code": i.code,
                "message": i.message,
                "section_index": i.section_index,
            }
            for i in report.issues
        ],
    }


def serialize_doctor(report: DoctorReport) -> dict:
    """``DoctorReport`` → 界面用的 dict。"""
    return {
        "ok": report.ok,
        "failed": len(report.failed),
        "warned": len(report.warned),
        "checks": [
            {
                "key": c.key,
                "title": c.title,
                "level": c.level,
                "level_label": LEVEL_LABELS.get(c.level, c.level).strip(),
                "detail": c.detail,
                "fix": c.fix if c.level in ("fail", "warn") else "",
                "extra": list(c.extra),
            }
            for c in report.checks
        ],
    }


# ------------------------------------------------------------------ 生成工作线程


class _Cancelled(Exception):
    """内部用：在 ``on_progress`` 里发现取消标志时抛出。"""


class GenerationWorker:
    """把一次生成跑在后台线程里，并把状态写回 ``TaskRecord``。"""

    def run(self, record: TaskRecord) -> None:
        """执行一次完整的生成：提交 → 等待 → 下载 → 写记录。"""
        try:
            record.status = "running"
            record.phase = "submitting"
            if not record.started_at:
                record.started_at = time.time()

            client = _client_factory(_base_url, HTTP_TIMEOUT)

            # --- 提交 ---
            try:
                upstream_id = client.submit(record.request_obj)
            except ServiceUnreachable as exc:
                self._fail(
                    record, "service_unreachable",
                    "出歌的程序还没打开，所以这次没能开始。", str(exc),
                )
                return
            except AceStepError as exc:
                self._fail(
                    record, "submit_failed",
                    "把任务交给出歌程序时出错了。", str(exc),
                )
                return

            record.upstream_task_id = upstream_id
            record.phase = "running"

            # --- 等待 ---
            def on_progress(_elapsed: float, _status: int) -> None:
                if record.cancel_flag.is_set():
                    raise _Cancelled()

            try:
                results = client.wait(
                    upstream_id,
                    timeout=DEFAULT_TIMEOUT,
                    interval=DEFAULT_INTERVAL,
                    on_progress=on_progress,
                )
            except _Cancelled:
                self._fail(record, "cancelled", "已停止等待这一首。", "")
                return
            except AceStepError as exc:
                message = str(exc)
                if "超时" in message or "timeout" in message.lower():
                    self._fail(
                        record, "timeout",
                        "等太久了，已停止等待。第一次出歌要加载模型，可能要一两分钟。",
                        message,
                    )
                else:
                    self._fail(record, "generate_failed", "这次没生成出来。", message)
                return

            # --- 下载 ---
            record.phase = "finalizing"
            saved: List[tuple] = []
            for index, result in enumerate(results):
                if len(results) == 1:
                    target = record.out_path
                else:
                    target = record.out_path.with_name(
                        f"{record.out_path.stem}_{index}{record.out_path.suffix}"
                    )
                try:
                    client.download(result.file_url, target)
                except AceStepError as exc:
                    self._fail(
                        record, "download_failed",
                        "歌出来了，但没能存到电脑上。", str(exc),
                    )
                    return
                saved.append((target, result))

            if not saved:
                self._fail(record, "download_failed", "没有文件被保存下来。", "")
                return

            elapsed = time.time() - record.started_at
            write_sidecar(record.out_path, record.request_obj, results, elapsed, record.preset)

            record.result = self._build_result(record, saved, results)
            record.status = "done"
            record.phase = "done"
            record.finished_at = time.time()
            store.mark_success()

        except Exception as exc:  # noqa: BLE001 —— 兜住一切，绝不把 traceback 递给用户
            self._fail(
                record, "generate_failed",
                "出错了，这次没生成出来。", f"{type(exc).__name__}: {exc}",
            )
        finally:
            if record.status in ("queued", "running"):
                record.status = "failed"
                if record.error is None:
                    record.error = {"code": "generate_failed", "message": "这次没生成出来。", "detail": ""}
            if not record.finished_at:
                record.finished_at = time.time()
            store.release_generation()

    def _fail(self, record: TaskRecord, code: str, message: str, detail: str) -> None:
        """把失败写进记录（status/phase/error）。中文人话 + 可选原始详情。"""
        record.status = "failed"
        record.phase = "failed"
        record.error = {"code": code, "message": message, "detail": detail or ""}
        record.finished_at = time.time()

    def _build_result(self, record: TaskRecord, saved: List[tuple], results: List[Any]) -> dict:
        """组装 ``status=done`` 时给前端的 result 对象。"""
        files = []
        for target, _result in saved:
            try:
                size = target.stat().st_size
            except OSError:
                size = 0
            name = target.name
            files.append({
                "name": name,
                "size_bytes": size,
                "media_url": "/media/" + urllib.parse.quote(name),
                "download_url": "/download/" + urllib.parse.quote(name),
            })

        request = record.request_obj
        reproduce = (
            f"sfs make --caption \"{request.prompt}\" "
            f"--lyrics-file <歌词文件> --duration {request.audio_duration:.0f} "
            + (f"--seed {request.seed} " if request.seed is not None else "")
        ).strip()

        first = results[0] if results else None
        # 注意：这里用「友好键名」（见 ARCH §3.3 的别名表），与前端 app.js 读取的键名一一对应。
        summary = {
            "duration": request.audio_duration,
            "tempo": request.bpm,
            "tune": request.key_scale,
            "steps": request.inference_steps,
            "versions": request.batch_size,
            "file_type": request.audio_format,
            "draft_first": request.thinking,
            "fixed_id": request.seed,
            "language": record.language,
            "language_label": record.language_label,
        }

        result: Dict[str, Any] = {
            "files": files,
            "reproduce": reproduce,
            "summary": summary,
            "params": request.to_payload(),
            "model": first.dit_model if first else "",
        }
        if first is not None:
            if first.duration is not None:
                result["duration"] = first.duration
            if first.bpm is not None:
                result["bpm"] = first.bpm
            if first.key_scale:
                result["key"] = first.key_scale
        return result


# ------------------------------------------------------------------ HTTP 处理


def _decode_target(target: str) -> tuple:
    """把 ``self.path`` 拆成 (解码后的路径, 查询参数 dict)。"""
    parsed = urllib.parse.urlsplit(target)
    path = urllib.parse.unquote(parsed.path)
    query = urllib.parse.parse_qs(parsed.query)
    return path, query


def _is_safe_name(name: str) -> bool:
    """文件名安全校验：不许有分隔符、不许 ``..``、不许空。"""
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    if os.path.basename(name) != name:
        return False
    return True


class RequestHandler(BaseHTTPRequestHandler):
    """单条 HTTP 请求的处理器。静态目录由 ``functools.partial`` 注入。"""

    server_version = f"song-for-someone/{__version__}"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: Any, directory: Optional[str] = None, **kwargs: Any) -> None:
        # BaseHTTPRequestHandler 不接受 directory；我们自己接住它。
        self.directory = directory or str(WEBAPP_DIR)
        super().__init__(*args, **kwargs)

    # 别把每条轮询都刷屏。
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    # ------------------------------------------------------------ 响应工具

    def _json(self, ok: bool, data: Any = None, error: Optional[dict] = None,
              status: int = 200) -> None:
        """统一信封：成功 ``{ok:true,data}``，失败 ``{ok:false,error}``。"""
        body: Dict[str, Any] = {"ok": bool(ok)}
        if ok:
            body["data"] = data
        else:
            body["error"] = error or {"code": "error", "message": "出错了。", "detail": ""}
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, status: int, code: str, message: str, detail: str = "",
               data: Any = None) -> None:
        """失败响应；``data`` 非空时一并带上（如歌词问题清单）。"""
        err = {"code": code, "message": message, "detail": detail or ""}
        if data is not None:
            err["data"] = data
        self._json(False, error=err, status=status)

    def _read_json_body(self) -> dict:
        """安全解析请求体的 JSON。非法则抛 ``ValueError``。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体不是合法的 JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("请求体必须是一个 JSON 对象")
        return parsed

    # ------------------------------------------------------------ 静态 / 媒体

    def _serve_static(self, path: str) -> None:
        """从 ``webapp/`` 提供静态资源。缺失时给极小兜底页。"""
        asset = STATIC_ROUTES.get(path)
        if asset is None:
            self._error(404, "not_found", "没有这个页面。")
            return
        full = Path(self.directory) / asset
        if not full.is_file():
            if asset == "index.html":
                self._send_bytes_from(_FALLBACK_HTML.encode("utf-8"),
                                      "text/html; charset=utf-8")
                return
            self._error(404, "not_found", "静态资源暂时找不着。")
            return
        try:
            data = full.read_bytes()
        except OSError:
            self._error(500, "not_found", "读静态资源时出错了。")
            return
        ctype = mimetypes.guess_type(str(full))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "image/svg+xml"):
            ctype += "; charset=utf-8"
        self._send_bytes_from(data, ctype)

    def _serve_media(self, name: str, as_download: bool) -> None:
        """提供 ``songs/`` 下的音频。支持 Range；下载走 attachment。"""
        if not _is_safe_name(name):
            self._error(403, "forbidden", "非法的文件名。")
            return
        target = Path(SONGS_DIR) / name
        if not target.is_file():
            self._error(404, "not_found", "这首歌找不着了。")
            return

        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        try:
            size = target.stat().st_size
        except OSError:
            self._error(404, "not_found", "这首歌找不着了。")
            return

        start, end, status = 0, max(0, size - 1), 200
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            spec = range_header[len("bytes="):].split(",")[0].strip()
            parts = spec.split("-", 1)
            try:
                if parts[0] == "":
                    # bytes=-N：最后 N 字节
                    length = int(parts[1])
                    start = max(0, size - length)
                    end = size - 1
                else:
                    start = int(parts[0])
                    end = int(parts[1]) if parts[1] else size - 1
            except (ValueError, IndexError):
                start, end = 0, size - 1
            if start > end or start >= size:
                start, end = 0, size - 1
            status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if as_download:
            quoted = urllib.parse.quote(name)
            self.send_header(
                "Content-Disposition",
                f"attachment; filename*=UTF-8''{quoted}",
            )
        self.end_headers()
        try:
            with open(target, "rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = handle.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_bytes_from(self, data: bytes, ctype: str) -> None:
        """发送一段内存字节（静态兜底页用）。"""
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ------------------------------------------------------------ GET

    def do_GET(self) -> None:  # noqa: N802
        try:
            path, query = _decode_target(self.path)
            if path == "/health":
                self._json(True, {"app": "song-for-someone", "version": __version__})
            elif path == "/api/meta":
                self._json(True, self._payload_meta())
            elif path == "/api/env":
                self._json(True, self._payload_env())
            elif path == "/api/doctor":
                self._handle_doctor()
            elif path == "/api/tasks/current":
                current = store.current()
                self._json(True, current.to_public() if current else None)
            elif path == "/api/songs":
                self._json(True, {"songs": self._payload_songs()})
            elif path == "/api/example":
                self._handle_example(query)
            elif path.startswith("/api/task/"):
                self._handle_task(path[len("/api/task/"):])
            elif path.startswith("/media/"):
                self._serve_media(path[len("/media/"):], as_download=False)
            elif path.startswith("/download/"):
                self._serve_media(path[len("/download/"):], as_download=True)
            elif path in STATIC_ROUTES:
                self._serve_static(path)
            else:
                self._error(404, "not_found", "没有这个页面。")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            self._error(500, "internal", "服务器内部出错了。", f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------ POST

    def do_POST(self) -> None:  # noqa: N802
        try:
            path, _query = _decode_target(self.path)
            if path == "/api/lyrics/check":
                self._handle_lyrics_check()
            elif path == "/api/generate":
                self._handle_generate()
            else:
                self._error(404, "not_found", "没有这个接口。")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # noqa: BLE001
            self._error(500, "internal", "服务器内部出错了。", f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------ 各接口实现

    def _payload_meta(self) -> dict:
        styles = []
        for preset in list_presets():
            styles.append({
                "key": preset.key,
                "name": preset.name,
                "prompt": preset.caption,
                "duration": preset.duration,
                "verified": preset.verified,
                "note": preset.note,
                "bpm": preset.bpm,
                "badge": "实测" if preset.verified else "建议",
            })
        return {
            "styles": styles,
            "durations": list(DURATION_OPTIONS),
            "lyrics_max_chars": MAX_LYRICS_CHARS,
            "examples": list(EXAMPLES),
        }

    def _payload_env(self) -> dict:
        """轻量环境快查：Python / 上游可达性 / 输出目录（目标 ≤2s）。"""
        parsed = urllib.parse.urlsplit(_base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8001

        reachable = False
        try:
            with socket.create_connection((host, port), timeout=0.3):
                reachable = True
        except OSError:
            reachable = False

        service_ok = reachable
        healthy = False
        if reachable:
            service_detail = "已连上出歌程序"
            try:
                client = _client_factory(_base_url, HEALTH_TIMEOUT)
                healthy = bool(client.health())
            except Exception:  # noqa: BLE001
                healthy = False
            if not healthy:
                service_detail = "出歌程序可能还在初始化"
        else:
            service_detail = "没连上出歌程序"

        out_dir_ok = True
        try:
            Path(SONGS_DIR).mkdir(parents=True, exist_ok=True)
        except OSError:
            out_dir_ok = False

        # 三态：上游不可达或目录不可写 → 不可用(bad)；连得上但还在初始化 → 需注意(warn)；否则正常(ok)。
        if not reachable or not out_dir_ok:
            level = "bad"
        elif not healthy:
            level = "warn"
        else:
            level = "ok"

        return {
            "python_ok": sys.version_info >= (3, 9),
            "service_ok": service_ok,
            "service_detail": service_detail,
            "out_dir_ok": out_dir_ok,
            "level": level,
        }

    def _handle_doctor(self) -> None:
        """完整自检。放工作线程里跑（``run_doctor`` 可能慢到 180s）。"""
        box: Dict[str, Any] = {}

        def job() -> None:
            try:
                box["report"] = serialize_doctor(_doctor_runner())
            except Exception as exc:  # noqa: BLE001
                box["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=job, daemon=True)
        thread.start()
        thread.join()

        if "report" in box:
            self._json(True, box["report"])
        else:
            self._error(500, "internal", "环境自检没能跑完。", box.get("error", ""))

    def _handle_lyrics_check(self) -> None:
        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._error(400, "bad_request", "请求格式不对。", str(exc))
            return
        lyrics = body.get("lyrics")
        if not isinstance(lyrics, str):
            lyrics = ""
        report = analyze_lyrics(lyrics)
        self._json(True, serialize_lyrics(report))

    def _handle_example(self, query: Dict[str, List[str]]) -> None:
        name = (query.get("name") or [""])[0]
        allowed = {item["key"] for item in EXAMPLES}
        if name not in allowed:
            self._error(404, "not_found", "没有这个示例。")
            return
        path = Path(EXAMPLES_DIR) / f"{name}.txt"
        if not path.is_file():
            self._error(404, "not_found", "示例文件找不着了。")
            return
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            self._error(404, "not_found", "示例文件读不了。")
            return
        self._json(True, {"name": name, "text": text})

    def _handle_task(self, task_id: str) -> None:
        record = store.get(task_id)
        if record is None:
            self._error(404, "not_found", "找不到这个任务。")
            return
        self._json(True, record.to_public())

    def _payload_songs(self) -> List[dict]:
        out_dir = Path(SONGS_DIR)
        if not out_dir.is_dir():
            return []
        audio = sorted(
            [p for p in out_dir.iterdir()
             if p.is_file() and p.suffix.lower() in (".mp3", ".wav", ".flac")],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        songs = []
        for path in audio:
            try:
                stat = path.stat()
            except OSError:
                continue
            meta: Dict[str, Any] = {}
            sidecar = path.with_suffix(".json")
            if sidecar.is_file():
                try:
                    loaded = json.loads(sidecar.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        meta = loaded
                except (OSError, json.JSONDecodeError):
                    meta = {}

            metas: Dict[str, Any] = {}
            results = meta.get("results") or []
            if results and isinstance(results[0], dict):
                metas = results[0].get("metas") or {}

            bpm_value = first_meta(metas, "bpm")
            key_value = first_meta(metas, "keyscale", "key_scale")
            request_raw = meta.get("request")

            songs.append({
                "name": path.name,
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
                "created_at": meta.get("created_at"),
                "elapsed_seconds": meta.get("elapsed_seconds"),
                "bpm": int(bpm_value) if isinstance(bpm_value, (int, float)) else None,
                "key": str(key_value) if key_value else None,
                "request": request_raw,
                "refill": _refill_from_request(request_raw),
                "media_url": "/media/" + urllib.parse.quote(path.name),
                "download_url": "/download/" + urllib.parse.quote(path.name),
            })
        return songs

    def _handle_generate(self) -> None:
        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._error(400, "bad_request", "请求格式不对。", str(exc))
            return

        prompt = str(body.get("prompt") or "").strip()
        lyrics = body.get("lyrics")
        if not isinstance(lyrics, str):
            lyrics = ""
        if not prompt or not lyrics.strip():
            self._error(400, "bad_request", "请先写好歌词、选好风格。")
            return

        # --- 时长 ---
        try:
            duration = float(body.get("duration", 120))
        except (TypeError, ValueError):
            self._error(422, "bad_duration", "时长请选 10 秒到 10 分钟之间。")
            return
        if duration < 10 or duration > 600:
            self._error(422, "bad_duration", "时长请选 10 秒到 10 分钟之间。")
            return

        # --- 歌词门禁（与 CLI 语义一致）---
        report = analyze_lyrics(lyrics)
        force = bool(body.get("force"))
        yes = bool(body.get("yes"))
        if report.errors and not force:
            self._error(
                409, "lyrics_error",
                f"歌词有 {len(report.errors)} 个必须改的地方，多半会唱不顺。",
                data={"issues": serialize_lyrics(report)["issues"]},
            )
            return
        if report.warnings and not yes and not force:
            self._error(
                409, "lyrics_warning",
                f"歌词有 {len(report.warnings)} 处建议改的地方。",
                data={"issues": serialize_lyrics(report)["issues"]},
            )
            return

        language = decide_language(report)

        # --- 抢锁 ---
        if not store.try_acquire_generation():
            current = store.current()
            self._error(
                409, "busy", BUSY_MESSAGE,
                data=current.to_public() if current else None,
            )
            return

        # --- 组装请求 ---
        try:
            request = self._build_request(body, prompt, lyrics, duration, language)
        except ValueError as exc:
            store.release_generation()
            self._error(400, "bad_request", "请求里有不认识的取值。", str(exc))
            return

        out_prefix = str(body.get("out_prefix") or "")
        out_stem = default_out_name(out_prefix)
        try:
            Path(SONGS_DIR).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            store.release_generation()
            self._error(500, "internal", "存歌的目录建不出来。", str(exc))
            return
        # 网页版会在同一分钟内连出多首（「再来一版」就是），必须避让已存在的文件，
        # 否则第二首会静默覆盖第一首。只改 web 侧的取名，不动 default_out_name()。
        out_name, out_path = _unique_output_path(Path(SONGS_DIR), out_stem, request.audio_format)

        estimated = estimate_seconds(request.audio_duration, request.batch_size, store.session_has_success)
        record = TaskRecord(
            task_id=uuid.uuid4().hex,
            status="queued",
            phase="queued",
            created_at=time.time(),
            started_at=time.time(),
            estimated_total=estimated,
            out_name=out_name,
            out_path=out_path,
            request_obj=request,
            request_payload=request.to_payload(),
            preset=get_preset(str(body.get("style_key") or "")),
            language=language,
            language_label=language_label(language),
        )
        store.create(record)

        worker = GenerationWorker()
        thread = threading.Thread(target=worker.run, args=(record,), daemon=True)
        thread.start()

        self._json(True, {
            "task_id": record.task_id,
            "estimated_total": round(estimated, 1),
            "out_name": out_name,
            "language": language,
            "language_label": language_label(language),
        }, status=202)

    def _build_request(self, body: dict, prompt: str, lyrics: str,
                       duration: float, language: str) -> GenerateRequest:
        """把界面传来的友好键名映射成 ``GenerateRequest``。"""
        audio_format = str(body.get("file_type", body.get("audio_format", "mp3")) or "mp3").lower()
        if audio_format not in AUDIO_FORMATS:
            audio_format = "mp3"

        def as_int(value: Any, default: Optional[int]) -> Optional[int]:
            if value is None or value == "":
                return default
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"不能把 {value!r} 当成整数") from exc

        def as_float(value: Any, default: float) -> float:
            if value is None or value == "":
                return default
            try:
                return float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"不能把 {value!r} 当成数字") from exc

        seed = as_int(body.get("fixed_id", body.get("seed")), None)
        bpm = as_int(body.get("tempo", body.get("bpm")), None)
        steps = as_int(body.get("steps", body.get("inference_steps")), 8)
        versions = as_int(body.get("versions", body.get("batch_size")), 1)
        thinking_choice = body.get("draft_first", body.get("thinking", True))

        if steps is None or steps < 1:
            steps = 8
        if versions is None or versions < 1:
            versions = 1
        if versions > 8:
            versions = 8

        key_scale = str(body.get("tune", body.get("key_scale", "")) or "")
        time_signature = str(body.get("time_signature", "") or "")

        return GenerateRequest(
            prompt=prompt,
            lyrics=lyrics,
            thinking=bool(thinking_choice),
            vocal_language=language,
            audio_duration=float(duration),
            inference_steps=int(steps),
            batch_size=int(versions),
            audio_format=audio_format,
            seed=seed,
            bpm=bpm,
            key_scale=key_scale,
            time_signature=time_signature,
            guidance_scale=as_float(body.get("guidance_scale"), 7.0),
            task_type=str(body.get("task_type") or "text2music"),
        )


def _refill_from_request(request_raw: Any) -> Optional[dict]:
    """把 ``songs/`` 里记录的原始请求映射成界面用的友好键名（「照这版再来一次」）。

    映射表复用 ``_ADVANCED_ALIASES``，避免和 ``_build_request`` 两处各写一份。
    """
    if not isinstance(request_raw, dict):
        return None
    defaults = {
        "fixed_id": None,
        "tempo": None,
        "tune": "",
        "steps": 8,
        "versions": 1,
        "file_type": "mp3",
        "draft_first": True,
    }
    refill = {
        "prompt": request_raw.get("prompt", ""),
        "lyrics": request_raw.get("lyrics", ""),
        "duration": request_raw.get("audio_duration", 120),
    }
    for friendly, canonical in _ADVANCED_ALIASES.items():
        refill[friendly] = request_raw.get(canonical, defaults[friendly])
    return refill


def _output_taken(out_dir: Path, name: str) -> bool:
    """目录里是否已经有这个文件名（音频或它的同名复现记录都算占位）。"""
    path = out_dir / name
    return path.exists() or path.with_suffix(".json").exists()


def _unique_output_path(out_dir: Path, stem: str, audio_format: str) -> tuple:
    """给本次出歌挑一个不会撞车的文件名。

    默认用 ``<stem>.<格式>``；若已存在则依次尝试 ``<stem>-2`` / ``-3`` …。
    这样同一分钟内连出多首（含「再来一版」）不会静默覆盖前一首。
    """
    candidate = f"{stem}.{audio_format}"
    if not _output_taken(out_dir, candidate):
        return candidate, out_dir / candidate
    index = 2
    while True:
        candidate = f"{stem}-{index}.{audio_format}"
        if not _output_taken(out_dir, candidate):
            return candidate, out_dir / candidate
        index += 1


# ------------------------------------------------------------------ 服务器


class _Server(ThreadingHTTPServer):
    """带 daemon 线程的 ThreadingHTTPServer。"""

    daemon_threads = True
    allow_reuse_address = True


def _make_server(host: str, port: int) -> _Server:
    """在指定地址上建服务器（handler 已注入静态目录）。"""
    handler = functools.partial(RequestHandler, directory=str(WEBAPP_DIR))
    return _Server((host, port), handler)


def pick_server(host: str, base_port: int, count: int) -> tuple:
    """从 ``base_port`` 起顺延试探，返回 (server, port)。

    先 ``connect`` 探测（占用则跳过），再尝试 ``bind``（失败也跳过）。
    全都失败返回 ``(None, None)``。
    """
    for offset in range(count):
        port = base_port + offset
        try:
            with socket.create_connection((host, port), timeout=0.3):
                continue  # 已经有人占用
        except OSError:
            pass
        try:
            return _make_server(host, port), port
        except OSError:
            continue
    return None, None


def _open_browser_when_ready(url: str, timeout: float = 10.0) -> None:
    """后台探测 ``/health``，就绪后打开浏览器。"""
    def probe() -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with net.urlopen(url + "health", timeout=0.5) as resp:
                    if resp.status == 200:
                        webbrowser.open(url)
                        return
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
    threading.Thread(target=probe, daemon=True).start()


def serve(host: str = HOST, base_port: int = DEFAULT_PORT,
          open_browser: bool = True, timeout: float = 10.0) -> int:
    """起服务并进入主循环。端口顺延；就绪后（可选）打开浏览器。"""
    server, port = pick_server(host, base_port, PORT_RANGE)
    if server is None:
        print(f"从 {base_port} 起试了 {PORT_RANGE} 个端口都被占用了。")
        print("多半是别的东西占着这些端口。关掉一些程序再试，或用 --port 指定别的端口。")
        return 1

    url = f"http://{host}:{port}/"
    print("=" * 60)
    print(f"  界面已启动： {url}")
    print("  （关掉这个窗口即停止；出歌期间可以关掉浏览器页面，任务会继续跑完）")
    print("=" * 60)
    sys.stdout.flush()

    if open_browser:
        _open_browser_when_ready(url, timeout=timeout)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """命令行入口：``python -m song_for_someone.web`` 或 ``python start.py``。"""
    global SONGS_DIR, _base_url

    parser = argparse.ArgumentParser(
        prog="song-for-someone-web",
        description="song-for-someone 的本地网页界面。",
    )
    parser.add_argument("--host", default=HOST, help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="起始端口（默认 8770）")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="ACE-Step 服务地址")
    parser.add_argument("--out-dir", default=None, help="出歌目录（默认仓库根的 songs/）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args(argv)

    _base_url = args.base_url
    if args.out_dir:
        SONGS_DIR = Path(args.out_dir).resolve()

    return serve(
        host=args.host,
        base_port=args.port,
        open_browser=not args.no_browser,
    )


if __name__ == "__main__":
    sys.exit(main())
