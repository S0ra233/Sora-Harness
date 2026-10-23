"""模型后端共用的 HTTP 边界；协议字段转换由各 Provider 完成。"""
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx

from ..contracts import TextSink
from ..messages import ReplayState, ToolCall
from ..models import ModelConfig, ModelResponse
from ..attempts import ModelAttemptSink
from ..diagnostics import RequestTrace
from ..usage import normalize_usage
from .retry import ProviderError, request_with_retries


StreamReader = Callable[[httpx.Response, TextSink], Awaitable[ModelResponse]]


@asynccontextmanager
async def _request_client(client: httpx.AsyncClient | None, timeout: httpx.Timeout) -> AsyncIterator[httpx.AsyncClient]:
    """借用运行内客户端；未传入时保留原有单次请求的创建和关闭方式。"""
    if client is not None:
        yield client
    else:
        async with httpx.AsyncClient(timeout=timeout) as owned_client:
            yield owned_client


async def _record_body(client, url, headers, payload, limits, trace, *, stream: bool):
    """仅开启诊断时准备一次JSON正文，重试发送同一字节串，不再次编码。"""
    if trace is None:
        return None, headers
    request = client.build_request("POST", url, headers=headers, json=payload, timeout=limits)
    body = request.content
    await trace.prepared(body, method=request.method, url=str(request.url), stream=stream)
    return body, {**headers, "Content-Type": request.headers["Content-Type"]}


async def post_json(
    url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float,
    *, client: httpx.AsyncClient | None = None, connect_timeout: float = 30.0,
    max_attempts: int = 5, on_attempt: ModelAttemptSink | None = None,
    trace: RequestTrace | None = None,
) -> dict[str, Any]:
    """复用显式传入的连接范围；协议解析仍交给 Provider。"""
    limits = httpx.Timeout(timeout, connect=connect_timeout)
    async with _request_client(client, limits) as active_client:
        body, send_headers = await _record_body(active_client, url, headers, payload, limits, trace, stream=False)
        async def send(on_text: TextSink) -> dict[str, Any]:
            if trace is None:
                response = await active_client.post(url, headers=headers, json=payload, timeout=limits)
            else:
                request = active_client.build_request("POST", url, headers=send_headers, content=body, timeout=limits)
                response = await active_client.send(request)
                trace.http_received(response.status_code)
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as exc:
                raise ProviderError("模型服务返回的内容不是有效 JSON") from exc
            if not isinstance(data, dict) or data.get("error"):
                raise ProviderError("模型服务返回了无效或失败的响应对象")
            return data
        return await request_with_retries(send, max_attempts=max_attempts, on_attempt=on_attempt, trace=trace)


async def read_stream(
    url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float,
    on_text: TextSink, reader: StreamReader,
    *, client: httpx.AsyncClient | None = None, connect_timeout: float = 30.0,
    max_attempts: int = 5, on_attempt: ModelAttemptSink | None = None,
    trace: RequestTrace | None = None,
) -> ModelResponse:
    """共用网络与 SSE 类型检查，完成条件交给实际协议的 reader。"""
    limits = httpx.Timeout(timeout, connect=connect_timeout)
    async with _request_client(client, limits) as active_client:
        send_headers = {**headers, "Accept": "text/event-stream"}
        body, send_headers = await _record_body(active_client, url, send_headers, payload, limits, trace, stream=True)
        async def consume(response, accept_text):
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "text/event-stream":
                raise ProviderError("接口未返回 SSE 流；请关闭逐段显示或确认接口支持流式响应。")
            return await reader(response, accept_text)
        async def send(accept_text: TextSink) -> ModelResponse:
            if trace is not None:
                request = active_client.build_request("POST", url, headers=send_headers, content=body, timeout=limits)
                response = await active_client.send(request, stream=True)
                try:
                    trace.http_received(response.status_code)
                    return await consume(response, accept_text)
                finally:
                    await response.aclose()
            async with active_client.stream(
                "POST", url, headers={**headers, "Accept": "text/event-stream"}, json=payload, timeout=limits,
            ) as response:
                return await consume(response, accept_text)
        return await request_with_retries(
            send, max_attempts=max_attempts, on_attempt=on_attempt, on_text=on_text, trace=trace,
        )


def model_response(
    text: str, usage: Any, reason: Any, status: str, native_data: dict[str, Any],
    *, tool_calls: list[ToolCall], config: ModelConfig, replay: dict[str, Any],
) -> ModelResponse:
    """合法空截断仍保留用量；完整成功须有效内容，截断调用不能交给执行层。"""
    if not isinstance(text, str) or (not text.strip() and not tool_calls and status != "incomplete"):
        raise ProviderError("响应未包含有效文本或工具调用")
    if usage is not None and not isinstance(usage, dict):
        raise ProviderError("响应中的用量信息不是对象")
    if reason is not None and not isinstance(reason, str):
        raise ProviderError("响应中的停止原因不是字符串")
    if tool_calls and status != "completed":
        raise ProviderError("工具调用未完整结束，不能执行或作为完整调用续接；请调整限制后重试。")
    if len({call.call_id for call in tool_calls}) != len(tool_calls):
        raise ProviderError("模型返回了重复的工具调用 ID。")
    return ModelResponse(
        text=text, usage=usage, finish_reason=reason,
        completion_status="requires_tool" if tool_calls else status, native_data=native_data,
        tool_calls=tool_calls,
        replay_state=ReplayState(config.api_style, config.endpoint_url, config.model, replay,
                                 provider_kind=config.provider_kind, credential_ref=config.credential_ref, key_env=config.key_env),
        token_usage=normalize_usage(usage, config.api_style, config.provider_kind),
    )


def parsed_tool_call(call_id: Any, name: Any, arguments: Any) -> ToolCall:
    """在远端响应边界检查字段形状，实际参数规则由之后的工具执行层负责。"""
    try:
        return ToolCall(call_id=call_id, name=name, arguments=arguments)
    except ValueError as exc:
        raise ProviderError("模型工具调用缺少有效 ID、名称或参数字符串。") from exc
