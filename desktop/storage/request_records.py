"""请求正文与尝试分开存储；可选诊断故障不进入模型重试通道。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections import Counter
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from ..llm.attempts import ModelAttemptEvent
from ..llm.diagnostics import RequestRecordNotice, RequestTraceContext
from ..llm.models import ModelRequest
from ..llm.messages import ModelMessage, ImageContent
from .atomic import write_text_atomic


_LOG = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: dict) -> None:
    write_text_atomic(path, json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2))


def _read_metadata(path: Path) -> dict:
    """索引与清单都是小文件；误放大正文时不在界面读取整份内容。"""
    with path.open("rb") as source:
        content = source.read(65537)
    if len(content) > 65536:
        raise ValueError("请求记录元数据超过64KiB")
    try:
        value = json.loads(content)
    except RecursionError as exc:
        raise ValueError("请求记录元数据嵌套过深") from exc
    if not isinstance(value, dict):
        raise ValueError("请求记录元数据不是对象")
    return value


def _safe_url(url: str) -> str:
    """只保存地址位置；不保存userinfo、query或fragment中的鉴权信息。"""
    address = urlsplit(url)
    host = address.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    if address.port is not None:
        host += f":{address.port}"
    return urlunsplit((address.scheme, host, address.path, "", ""))


class RequestRecordStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.expanduser().resolve()

    def begin(self, context: RequestTraceContext, *, api_style: str, model: str,
              prompt_source: dict | None = None,
              on_notice: Callable[[RequestRecordNotice], None] | None = None) -> RequestRecordRun:
        """仅创建本轮对象，进入async with才写盘；关闭功能时不调用本方法。"""
        return RequestRecordRun(self, context, api_style, model, prompt_source, on_notice)

    @asynccontextmanager
    async def recording(self, enabled: bool, context: RequestTraceContext, *, api_style: str, model: str,
                        prompt_source: dict | None = None,
                        on_notice: Callable[[RequestRecordNotice], None] | None = None):
        """GUI、CLI与手动摘要共用开关；关闭时不创建记录对象或目录。"""
        if not enabled:
            yield None
            return
        async with self.begin(context, api_style=api_style, model=model,
                              prompt_source=prompt_source, on_notice=on_notice) as run:
            yield run

    def latest(self, conversation_id: str) -> tuple[str | None, str | None]:
        """后台读取小索引，不扫描或解析历史正文／图片。"""
        identifier = UUID(conversation_id).hex
        path = self.directory / "latest" / (identifier + ".json")
        try:
            if not path.exists():
                return None, None
            value = _read_metadata(path)
            if value.get("conversation_id") != identifier:
                raise ValueError("记录索引归属不匹配")
            run_id = UUID(value["run_id"]).hex
            directory = self.directory_for(identifier, run_id)
            warning = _read_metadata(directory / "run.json").get("warning")
            return run_id, warning if isinstance(warning, str) else None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return None, f"请求记录索引不可读（{type(exc).__name__}），原文件保留。"

    def directory_for(self, conversation_id: str, run_id: str) -> Path:
        identifier, run_id = UUID(conversation_id).hex, UUID(run_id).hex
        path = self.directory / run_id
        if path.is_symlink() or not path.is_dir() or path.resolve(strict=True).parent != self.directory:
            raise ValueError("请求记录目录不存在或已变成链接。")
        value = _read_metadata(path / "run.json")
        if value.get("conversation_id") != identifier or value.get("run_id") != run_id:
            raise ValueError("请求记录不属于当前会话。")
        return path


class RequestRecordRun:
    def __init__(self, store, context, api_style, model, prompt_source, on_notice) -> None:
        self.context = replace(context, run_id=UUID(context.run_id).hex,
                               conversation_id=UUID(context.conversation_id).hex if context.conversation_id else None)
        self.store, self.api_style, self.model = store, api_style, model
        self.directory = store.directory / self.context.run_id
        self.prompt_source, self._on_notice = prompt_source, on_notice
        self._lock = asyncio.Lock()
        self._pending: list[tuple[asyncio.Task, str, bool]] = []
        self._disabled = False
        self._owned_directory = False
        self._available = False
        self._index_attempted = False
        self.warning: str | None = None
        self._captures: list[RequestCapture] = []
        self._run = {"version": 1, **asdict(self.context), "created_at": _now(), "status": "recording",
                     "api_style": api_style, "model": model, "request_count": 0, "warning": None}

    async def __aenter__(self):
        try:
            await self.io(self._start, "开始记录")
            if not self._disabled and self.context.conversation_id is not None:
                self._index_attempted = True
                await self.io(self._publish_index, "更新请求索引", critical=False)
            self._emit()
            return self
        except BaseException as exc:
            # __aenter__未完成时Python不会调用__aexit__，这里也必须回收已启动的写盘。
            await self.__aexit__(type(exc), exc, exc.__traceback__)
            raise

    async def __aexit__(self, kind, error, traceback):
        cleanup = asyncio.create_task(self._finish_record(error))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # 结束写盘中收到取消也先回收；不能让worker终态越过未完成IO。
            await asyncio.shield(cleanup)
            raise

    async def _finish_record(self, error: BaseException | None) -> None:
        status = ("cancelled" if isinstance(error, asyncio.CancelledError) else "failed") if error else self._run.get("outcome", "completed")
        for capture in self._captures:
            if not capture.finished:
                await capture.finish("cancelled" if status == "cancelled" else "interrupted")
        def finish():
            self._run.update(status=status, finished_at=_now(), request_count=len(self._captures), warning=self.warning)
            _write_json(self.directory / "run.json", self._run)
            self._available = True
        await self.io(finish, "结束记录", final=True)
        if self._available and not self._index_attempted and self.context.conversation_id is not None:
            self._index_attempted = True
            await self.io(self._publish_index, "更新请求索引", critical=False)
        self._emit()

    def set_outcome(self, status: str) -> None:
        self._run["outcome"] = status

    def _start(self) -> None:
        self.directory.parent.mkdir(parents=True, exist_ok=True)
        self.directory.mkdir(exist_ok=False)
        self._owned_directory = True
        _write_json(self.directory / "run.json", self._run)
        self._available = True

    def _publish_index(self) -> None:
        index = self.store.directory / "latest" / (self.context.conversation_id + ".json")
        if index.exists():
            old = _read_metadata(index)
            if old.get("conversation_id") != self.context.conversation_id:
                raise ValueError("原请求索引损坏，不能覆盖")
            UUID(old["run_id"])
        _write_json(index, {"version": 1, "conversation_id": self.context.conversation_id,
                            "run_id": self.context.run_id})

    def for_request(self, request: ModelRequest, *, purpose: str, step: int | None = None,
                    compaction_id: str | None = None, after_step: int | None = None,
                    plugin_id: str | None = None, action: str | None = None,
                    owner_run_id: str | None = None) -> RequestCapture:
        context = replace(self.context, purpose=purpose, step=step, compaction_id=compaction_id,
                          after_step=after_step, plugin_id=plugin_id, action=action, owner_run_id=owner_run_id)
        capture = RequestCapture(self, context, request)
        self._captures.append(capture)
        return capture

    async def io(self, operation, stage: str, *, final: bool = False, critical: bool = True) -> bool:
        """保留被取消的写入任务，结算前先等待它；不会留下后台写盘越过结束状态。"""
        async with self._lock:
            success = await self._drain()
            if self._disabled and not final:
                return False
            if final and not self._owned_directory:
                return False
            self._pending.append((asyncio.create_task(asyncio.to_thread(operation)), stage, critical))
            return await self._drain() and success

    async def _drain(self) -> bool:
        success = True
        while self._pending:
            task, stage, critical = self._pending[0]
            try:
                await asyncio.shield(task)
            except Exception as exc:
                success = False
                if critical:
                    self._disabled = True
                self.warning = f"请求诊断不完整：{stage}失败（{type(exc).__name__}）。正常对话继续。"
                _LOG.error("请求诊断写入失败：运行=%s 阶段=%s 类型=%s errno=%s winerror=%s", self.context.run_id,
                           stage, type(exc).__name__, getattr(exc, "errno", None), getattr(exc, "winerror", None))
                self._emit()
            finally:
                if task.done():
                    self._pending.pop(0)
        return success

    def _emit(self) -> None:
        if self._on_notice is not None:
            try:
                self._on_notice(RequestRecordNotice(self.context.run_id, self._available, self.warning))
            except Exception:
                _LOG.error("请求诊断通知失败：运行=%s", self.context.run_id)


class RequestCapture:
    def __init__(self, run: RequestRecordRun, context: RequestTraceContext, request: ModelRequest) -> None:
        self.run, self.request_id = run, uuid4().hex
        self.directory = run.directory / self.request_id
        self._created, self.finished = False, False
        self._attempt = 0
        self._http_events: list[dict] = []
        self._manifest = {
            "version": 1, **asdict(context), "request_id": self.request_id, "created_at": _now(),
            "api_style": run.api_style, "model": run.model, "status": "preparing", "body_saved": False,
            "configured_prompt_source": run.prompt_source,
            "unified_request_view": {"message_count": len(request.messages), "tool_names": [tool.name for tool in request.tools],
                "roles": dict(Counter(message.role for message in request.messages if isinstance(message, ModelMessage))),
                "image_references": sum(isinstance(part, ImageContent) for message in request.messages
                                        if isinstance(message, ModelMessage) for part in message.content),
                "max_output_tokens": request.max_output_tokens, "reasoning_effort": request.reasoning_effort,
                "tool_choice": request.tool_choice},
        }

    def _ensure(self) -> None:
        if not self._created:
            self.directory.mkdir(exist_ok=False)
            self._created = True

    def _save_manifest(self) -> None:
        self._ensure()
        _write_json(self.directory / "manifest.json", self._manifest)

    async def prepared(self, body: bytes, *, method: str, url: str, stream: bool) -> None:
        def write():
            self._manifest.update(method=method, endpoint=_safe_url(url), stream=stream)
            # 指纹只用于解释观测到的变化，不推断服务端缓存命中或保存额外正文。
            payload = json.loads(body)
            components = {}
            if isinstance(payload, dict):
                for key in ("tools", "system", "systemInstruction", "instructions", "messages", "input", "contents"):
                    if key in payload:
                        encoded = json.dumps(payload[key], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                        components[key] = {"bytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}
            self._manifest["components"] = components
            self._save_manifest()
            # 独占写入，不覆盖已有原件；完整性以manifest.body_saved为准。
            with (self.directory / "body.json").open("xb") as output:
                view = memoryview(body)
                for offset in range(0, len(view), 1024 * 1024):
                    output.write(view[offset:offset + 1024 * 1024])
                output.flush()
                os.fsync(output.fileno())
            self._manifest.update(body_saved=True, bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
                                  sha256_scope="http_entity_body", status="prepared")
            self._save_manifest()
        await self.run.io(write, "保存请求正文")

    async def attempt(self, event: ModelAttemptEvent) -> None:
        self._attempt = event.attempt
        while self._http_events:
            await self._append(self._http_events.pop(0))
        await self._append({"request_id": event.request_id, "attempt": event.attempt, "phase": event.phase,
                            "retrying": event.retrying, "error": event.error, "duration_seconds": event.duration_seconds})

    def http_received(self, status: int) -> None:
        # 这里只登记小事件；写盘放在计时结束后的attempt通知，避免混入TPS耗时。
        self._http_events.append({"timestamp": _now(), "request_id": self.request_id, "attempt": self._attempt,
                                 "phase": "http_received", "http_status": status})

    async def _append(self, value: dict) -> None:
        value = {"timestamp": _now(), **value}
        def write():
            self._ensure()
            with (self.directory / "attempts.jsonl").open("a", encoding="utf-8", newline="\n") as output:
                output.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
                output.flush()
                os.fsync(output.fileno())
        await self.run.io(write, "保存请求尝试")

    async def unavailable(self) -> None:
        self._manifest["capture_unavailable"] = "backend_not_supported"
        self.run.warning = "当前模型后端不支持请求正文记录，已标记缺失；正常对话继续。"
        self.run._emit()
        await self.run.io(self._save_manifest, "保存后端记录能力")

    async def finish(self, status: str, *, error_type=None, completion_status=None) -> None:
        while self._http_events:
            await self._append(self._http_events.pop(0))
        def finish():
            self._manifest.update(status=status, finished_at=_now(), error_type=error_type,
                                  model_completion_status=completion_status, attempts=self._attempt,
                                  record_warning=self.run.warning)
            self._save_manifest()
        await self.run.io(finish, "结算请求", final=True)
        self.finished = True
