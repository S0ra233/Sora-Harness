"""读取 Chat Completions 的 SSE 事件，组装文本及实际返回的元数据。"""
from typing import Any

import httpx

from ...contracts import TextSink, ReasoningSink
from ...reasoning_output import ReasoningDelta
from ...models import ModelConfig, ModelResponse
from ..stream_events import StreamFormatError, StreamInterruptedError, iter_event_data as _iter_event_data, parse_event_data, mark_stream_output
from .response import parse_chat_response


async def read_chat_stream(response: httpx.Response, on_text: TextSink, *, config: ModelConfig,
                           on_reasoning: ReasoningSink | None = None) -> ModelResponse:
    """片段立即通知调用方；只有收到结束标记才返回完整响应。"""
    parts: list[str] = []
    finish_reason = None
    usage = None
    chunks = []
    calls: dict[int, dict[str, Any]] = {}
    reasoning = []
    reasoning_alias = []
    details = {}
    details_public = False
    async for event_data in _iter_event_data(response):
        if event_data.strip() == "[DONE]":
            text = "".join(parts)
            message: dict[str, Any] = {"role": "assistant", "content": text}
            if calls:
                message["tool_calls"] = [calls[index] for index in sorted(calls)]
            if reasoning:
                message["reasoning_content"] = "".join(reasoning)
            if reasoning_alias:
                message["reasoning"] = "".join(reasoning_alias)
            if details:
                message["reasoning_details"] = list(details.values())
            result = parse_chat_response({
                "choices": [{"message": message, "finish_reason": finish_reason}], "usage": usage,
            }, config)
            result.native_data = {"chunks": chunks}
            return result
        chunk = parse_event_data(event_data)
        chunks.append(chunk)
        text_delta, chunk_reason, chunk_usage = _parse_chunk(chunk)
        if chunk["choices"]:
            delta = chunk["choices"][0]["delta"]
            public_details = delta.get("reasoning_details")
            has_public_details = isinstance(public_details, list) and any(isinstance(item, dict)
                and item.get("type") in {"reasoning.text", "reasoning.summary"} for item in public_details)
            details_public = details_public or has_public_details
            _collect_calls(calls, delta.get("tool_calls"))
            if delta.get("tool_calls"):
                mark_stream_output()
            if delta.get("reasoning_content") is not None:
                value = delta["reasoning_content"]
                if not isinstance(value, str):
                    raise StreamFormatError("Chat 推理增量不是字符串。")
                reasoning.append(value)
                if value:
                    mark_stream_output()
                if value and on_reasoning is not None and not details_public:
                    on_reasoning(ReasoningDelta("chat", value))
            if delta.get("reasoning") is not None:
                value = delta["reasoning"]
                if not isinstance(value, str):
                    raise StreamFormatError("Chat reasoning 增量不是字符串。")
                reasoning_alias.append(value)
                if value:
                    mark_stream_output()
                if value and on_reasoning is not None and not delta.get("reasoning_content") and not details_public:
                    on_reasoning(ReasoningDelta("chat", value))
            _collect_reasoning(details, delta.get("reasoning_details"), on_reasoning)
        if text_delta:
            parts.append(text_delta)
            on_text(text_delta)
        if chunk_reason is not None:
            finish_reason = chunk_reason
        if chunk_usage is not None:
            usage = chunk_usage
    raise StreamInterruptedError("流式连接在完整结束标记前断开；已显示文本不能作为完整结果。")


def _collect_reasoning(details, incoming, on_reasoning) -> None:
    """按原生索引／ID拼装详情；加密内容只保存，不作为公开文本通知。"""
    if incoming is None:
        return
    if not isinstance(incoming, list):
        raise StreamFormatError("Chat reasoning_details 增量不是数组。")
    for position, delta in enumerate(incoming):
        if not isinstance(delta, dict):
            raise StreamFormatError("Chat 推理详情增量不是对象。")
        key = delta.get("index", delta.get("id", position))
        if not isinstance(key, (int, str)) or isinstance(key, bool):
            raise StreamFormatError("Chat 推理详情索引无效。")
        block = details.setdefault(key, {})
        for name, value in delta.items():
            if name in {"text", "summary", "data", "signature"}:
                if not isinstance(value, str):
                    raise StreamFormatError("Chat 推理详情片段不是字符串。")
                block[name] = block.get(name, "") + value
                if value:
                    mark_stream_output()
            else:
                block[name] = value
        kind = block.get("type")
        text = delta.get("summary") if kind == "reasoning.summary" else delta.get("text") if kind == "reasoning.text" else None
        if on_reasoning is not None and kind in {"reasoning.text", "reasoning.summary", "reasoning.encrypted"}:
            on_reasoning(ReasoningDelta(str(key), text or "",
                                       "delta" if text else "started",
                                       "summary" if block.get("type") == "reasoning.summary" else "reasoning"))


def _parse_chunk(event_data: str | dict[str, Any]) -> tuple[str, str | None, dict[str, Any] | None]:
    """读取单个文本片段；空 choices 可以是单独的用量事件。"""
    data = parse_event_data(event_data) if isinstance(event_data, str) else event_data
    usage = data.get("usage")
    if usage is not None and not isinstance(usage, dict):
        raise StreamFormatError("流式事件中的 usage 不是对象。")
    choices = data.get("choices")
    if not isinstance(choices, list):
        raise StreamFormatError("流式事件缺少 choices 数组。")
    if not choices:
        return "", None, usage
    choice = choices[0]
    if not isinstance(choice, dict) or not isinstance(choice.get("delta"), dict):
        raise StreamFormatError("流式事件缺少 choices[0].delta 对象。")
    delta = choice["delta"]
    if delta.get("function_call"):
        raise StreamFormatError("旧版流式 function_call 尚未支持。")
    if delta.get("refusal"):
        raise StreamFormatError("模型拒绝了这次请求。")
    text = delta.get("content")
    if text is not None and not isinstance(text, str):
        raise StreamFormatError("流式文本片段不是字符串。")
    reason = choice.get("finish_reason")
    if reason == "function_call":
        raise StreamFormatError("旧版流式 function_call 尚未支持。")
    if reason is not None and not isinstance(reason, str):
        raise StreamFormatError("流式事件中的 finish_reason 不是字符串。")
    return text or "", reason, usage


def _collect_calls(calls: dict[int, dict[str, Any]], deltas: Any) -> None:
    """按 index 组装交错的调用，参数字符串在收到结束标记前不解析。"""
    if deltas is None:
        return
    if not isinstance(deltas, list):
        raise StreamFormatError("Chat 工具增量不是数组。")
    for delta in deltas:
        if not isinstance(delta, dict) or not isinstance(delta.get("index"), int):
            raise StreamFormatError("Chat 工具增量缺少 index。")
        call = calls.setdefault(delta["index"], {"function": {"name": "", "arguments": ""}})
        for field in ("id", "type", "extra_content"):
            if field in delta:
                call[field] = delta[field]
        function = delta.get("function", {})
        if not isinstance(function, dict):
            raise StreamFormatError("Chat 工具增量的 function 不是对象。")
        for field in ("name", "arguments"):
            if field in function:
                if not isinstance(function[field], str):
                    raise StreamFormatError("Chat 工具名称或参数增量不是字符串。")
                call["function"][field] += function[field]
