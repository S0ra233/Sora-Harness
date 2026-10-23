"""可选请求观察接口；记录对象不进入ModelRequest或厂商JSON。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .attempts import ModelAttemptEvent, ModelAttemptSink
    from .contracts import ModelBackend, ReasoningSink, TextSink
    from .models import ModelRequest, ModelResponse


@dataclass(frozen=True)
class RequestTraceContext:
    run_id: str
    conversation_id: str | None = None
    purpose: str = "agent"
    step: int | None = None
    compaction_id: str | None = None
    after_step: int | None = None
    plugin_id: str | None = None
    action: str | None = None
    owner_run_id: str | None = None


@dataclass(frozen=True)
class RequestRecordNotice:
    run_id: str
    available: bool
    warning: str | None = None


class RequestTrace(Protocol):
    request_id: str

    async def prepared(self, body: bytes, *, method: str, url: str, stream: bool) -> None: ...
    async def attempt(self, event: ModelAttemptEvent) -> None: ...
    def http_received(self, status: int) -> None: ...
    async def finish(self, status: str, *, error_type: str | None = None,
                     completion_status: str | None = None) -> None: ...
    async def unavailable(self) -> None: ...


class RequestTraceFactory(Protocol):
    def for_request(self, request: ModelRequest, *, purpose: str, step: int | None = None,
                    compaction_id: str | None = None, after_step: int | None = None,
                    plugin_id: str | None = None, action: str | None = None,
                    owner_run_id: str | None = None) -> RequestTrace: ...


async def call_observed_model(
    backend: ModelBackend, request: ModelRequest, *, on_text: TextSink | None = None,
    on_attempt: ModelAttemptSink | None = None, on_reasoning: ReasoningSink | None = None,
    trace: RequestTrace | None = None,
) -> ModelResponse:
    """共同结算Provider解析后的状态；HTTP收到响应不等于合法模型响应。"""
    options = {}
    if on_attempt is not None:
        options["on_attempt"] = on_attempt
    if on_reasoning is not None and getattr(backend, "supports_reasoning_events", False):
        options["on_reasoning"] = on_reasoning
    if trace is not None:
        if getattr(backend, "supports_request_trace", False):
            options["trace"] = trace
        else:
            await trace.unavailable()
    try:
        response = (await backend.complete(request, **options) if on_text is None else
                    await backend.stream_complete(request, on_text, **options))
    except BaseException as exc:
        if trace is not None:
            await trace.finish("cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                               error_type=type(exc).__name__)
        raise
    if trace is not None:
        await trace.finish("response_received", completion_status=response.completion_status)
    return response
