"""Anthropic Messages 消息与工具协议；推理与签名内容原样续接。"""
import json
from functools import partial
from typing import Any

import httpx
from ...attachments.store import ImageStore

from ..contracts import TextSink, ReasoningSink
from ..diagnostics import RequestTrace
from ..reasoning_output import ReasoningDelta
from ..attempts import ModelAttemptSink
from ..models import ModelConfig, ModelRequest, ModelResponse
from ..history_projection import project_request, consume_projected_request
from ..validation import resolve_model_config
from ..reasoning import MESSAGES_DEFAULT_MAX_TOKENS
from .http import ProviderError, model_response, parsed_tool_call, post_json, read_stream
from .protocol_messages import anthropic_messages
from .reasoning import reasoning_fields, reasoning_attempts
from .image_input import prepare_request_images
from .stream_events import StreamFormatError, StreamInterruptedError, iter_event_data, parse_event_data, mark_stream_output


class AnthropicProvider:
    supports_reasoning_events = True
    supports_request_trace = True
    def __init__(self, config: ModelConfig, api_key: str, *, client: httpx.AsyncClient | None = None,
                 attachments: ImageStore | None = None):
        self.config = resolve_model_config(config)
        self._api_key = api_key
        self._client = client
        self._attachments = attachments

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._api_key, "anthropic-version": "2023-06-01"}

    async def complete(self, request: ModelRequest, *, on_attempt: ModelAttemptSink | None = None,
                       on_reasoning: ReasoningSink | None = None, trace: RequestTrace | None = None) -> ModelResponse:
        request, prefix_hash = project_request(request, self.config)
        data = await post_json(
            self.config.endpoint_url, self._headers(),
            await self._request_payload(request, stream=False), self.config.timeout_seconds, client=self._client,
            connect_timeout=self.config.connect_timeout_seconds, max_attempts=self.config.max_attempts,
            on_attempt=reasoning_attempts(self.config, request, on_attempt, on_reasoning),
            trace=trace,
        )
        response = _parse_response(data, self.config)
        response.replay_state.request_prefix_hash = prefix_hash
        return response

    async def stream_complete(
        self, request: ModelRequest, on_text: TextSink, *, on_attempt: ModelAttemptSink | None = None,
        on_reasoning: ReasoningSink | None = None,
        trace: RequestTrace | None = None,
    ) -> ModelResponse:
        request, prefix_hash = project_request(request, self.config)
        response = await read_stream(
            self.config.endpoint_url, self._headers(),
            await self._request_payload(request, stream=True), self.config.timeout_seconds,
            on_text, partial(_read_stream, config=self.config, on_reasoning=on_reasoning), client=self._client,
            connect_timeout=self.config.connect_timeout_seconds, max_attempts=self.config.max_attempts,
            on_attempt=reasoning_attempts(self.config, request, on_attempt, on_reasoning),
            trace=trace,
        )
        response.replay_state.request_prefix_hash = prefix_hash
        return response

    async def _request_payload(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        request, _ = consume_projected_request(request, self.config)
        reasoning = reasoning_fields(self.config, request)
        images = await prepare_request_images(request, self.config, self._attachments)
        systems, turns = anthropic_messages(request.messages, self.config, images, projected=True)
        payload = {
            "model": self.config.model, "messages": turns, "stream": stream,
            # Messages 要求必填；其他协议留空时不发送输出上限。
            "max_tokens": request.max_output_tokens if request.max_output_tokens is not None else MESSAGES_DEFAULT_MAX_TOKENS,
        }
        if systems:
            payload["system"] = "\n\n".join(systems)
        if request.tools:
            payload["tools"] = [
                {"name": tool.name, "description": tool.description, "input_schema": tool.parameters}
                for tool in request.tools
            ]
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tool_choice is not None and request.tools:
            payload["tool_choice"] = {"type": request.tool_choice}
        payload.update(reasoning)
        if self.config.provider_rules.cache_control:
            from .cache_control import mark_anthropic_prefix
            mark_anthropic_prefix(payload, auxiliary=request.tool_choice == "none")
        return payload


def _check_block(block: Any) -> None:
    if not isinstance(block, dict) or block.get("type") not in ("text", "thinking", "redacted_thinking", "tool_use"):
        raise ProviderError("当前只支持 Messages 文本、推理与客户端 tool_use，不支持服务端工具或其他内容块。")
    if block["type"] == "text" and not isinstance(block.get("text"), str):
        raise ProviderError("Messages 文本内容不是字符串。")


def _parse_response(data: dict[str, Any], config: ModelConfig) -> ModelResponse:
    content = data.get("content")
    if not isinstance(content, list):
        raise ProviderError("Messages 响应缺少 content 数组。")
    parts = []
    calls = []
    for block in content:
        _check_block(block)
        if block["type"] == "text":
            parts.append(block["text"])
        elif block["type"] == "tool_use":
            if not isinstance(block.get("input"), dict):
                raise ProviderError("Messages 工具 input 不是对象。")
            calls.append(parsed_tool_call(block.get("id"), block.get("name"), json.dumps(block["input"], ensure_ascii=False)))
    reason = data.get("stop_reason")
    if reason == "refusal":
        raise ProviderError("模型拒绝了这次请求。")
    if reason == "pause_turn":
        raise ProviderError("Messages 返回服务端工具 pause_turn；当前尚未支持服务端工具续接。")
    if reason == "tool_use" and not calls:
        raise ProviderError("Messages 以 tool_use 结束，但没有返回工具调用。")
    status = (
        "completed" if reason in ("end_turn", "stop_sequence", "tool_use")
        else "incomplete" if reason in ("max_tokens", "model_context_window_exceeded")
        else "unknown"
    )
    return model_response(
        "".join(parts), data.get("usage"), reason, status, data,
        tool_calls=calls, config=config, replay={"content": content},
    )


def _apply_block_delta(block: dict[str, Any], delta: Any, on_text: TextSink,
                       on_reasoning: ReasoningSink | None = None, block_id: str = "0") -> None:
    """正文与公开思考分别通知 UI；签名只保留在原生回放。"""
    if not isinstance(delta, dict):
        raise StreamFormatError("Messages 内容增量不是对象。")
    kind = delta.get("type")
    fields = {
        "text_delta": ("text", "text"),
        "thinking_delta": ("thinking", "thinking"),
        "signature_delta": ("thinking", "signature"),
    }
    if kind == "citations_delta" and block["type"] == "text":
        block.setdefault("citations", []).append(delta.get("citation"))
        return
    if kind not in fields or block["type"] != fields[kind][0]:
        raise StreamFormatError("Messages 的内容类型与增量不匹配，或该增量尚未支持。")
    field = fields[kind][1]
    value = delta.get(field)
    if not isinstance(value, str):
        raise StreamFormatError("Messages 内容增量不是字符串。")
    block[field] = block.get(field, "") + value
    if value:
        mark_stream_output()
    if field == "text" and value:
        on_text(value)
    elif field == "thinking" and value and on_reasoning is not None:
        on_reasoning(ReasoningDelta(block_id, value))


async def _read_stream(response: httpx.Response, on_text: TextSink, *, config: ModelConfig,
                       on_reasoning: ReasoningSink | None = None) -> ModelResponse:
    message = None
    blocks: dict[int, dict[str, Any]] = {}
    tool_json: dict[int, list[str]] = {}
    closed: set[int] = set()
    async for event_data in iter_event_data(response):
        event = parse_event_data(event_data)
        kind = event.get("type")
        if kind == "ping":
            continue
        if kind == "message_start":
            initial = event.get("message")
            if message is not None or not isinstance(initial, dict):
                raise StreamFormatError("Messages 起始事件缺少有效 message，或重复开始。")
            usage = initial.get("usage")
            if usage is not None and not isinstance(usage, dict):
                raise StreamFormatError("Messages 起始事件中的 usage 不是对象。")
            message = {**initial, "usage": dict(usage) if usage is not None else None}
            continue
        if message is None:
            raise StreamFormatError("Messages 流缺少 message_start 起始事件。")
        if kind == "content_block_start":
            block = event.get("content_block")
            _check_block(block)
            index = event.get("index")
            if not isinstance(index, int) or index in blocks:
                raise StreamFormatError("Messages 内容块索引无效或重复。")
            blocks[index] = dict(block)
            if block["type"] == "tool_use" or block.get("thinking") or block.get("data"):
                mark_stream_output()
            if block["type"] in {"thinking", "redacted_thinking"} and on_reasoning is not None:
                on_reasoning(ReasoningDelta(str(index), block.get("thinking", "") if block["type"] == "thinking" else "", "started"))
            if block["type"] == "tool_use":
                tool_json[index] = []
            if block["type"] == "text" and block.get("text"):
                on_text(block["text"])
        elif kind == "content_block_delta":
            index = event.get("index")
            if not isinstance(index, int) or index not in blocks or index in closed:
                raise StreamFormatError("Messages 增量引用了尚未开始的内容块。")
            delta = event.get("delta")
            if blocks[index]["type"] == "tool_use":
                if not isinstance(delta, dict) or delta.get("type") != "input_json_delta" or not isinstance(delta.get("partial_json"), str):
                    raise StreamFormatError("Messages 工具参数增量不是 input_json_delta 字符串。")
                tool_json[index].append(delta["partial_json"])
                if delta["partial_json"]:
                    mark_stream_output()
            else:
                _apply_block_delta(blocks[index], delta, on_text, on_reasoning, str(index))
        elif kind == "content_block_stop":
            index = event.get("index")
            if not isinstance(index, int) or index not in blocks or index in closed:
                raise StreamFormatError("Messages 内容块结束索引无效。")
            closed.add(index)
            if blocks[index]["type"] in {"thinking", "redacted_thinking"} and on_reasoning is not None:
                on_reasoning(ReasoningDelta(str(index), status="finished"))
            if index in tool_json and tool_json[index]:
                try:
                    blocks[index]["input"] = json.loads("".join(tool_json[index]))
                except ValueError as exc:
                    raise StreamFormatError("Messages 工具参数在内容块结束时仍不是完整 JSON。") from exc
        elif kind == "message_delta":
            delta, usage = event.get("delta"), event.get("usage")
            if not isinstance(delta, dict) or (usage is not None and not isinstance(usage, dict)):
                raise StreamFormatError("Messages 结束增量或用量不是对象。")
            message.update(delta)
            # output_tokens 是累计值；与起始 input_tokens 合并，不能逐次相加。
            if usage is not None:
                message["usage"] = {**(message["usage"] or {}), **usage}
        elif kind == "message_stop":
            if not message.get("stop_reason"):
                raise StreamFormatError("Messages 结束事件缺少 stop_reason。")
            if set(blocks) != closed:
                raise StreamFormatError("Messages 结束时仍有未收齐的内容块。")
            message["content"] = [blocks[index] for index in sorted(blocks)]
            result = _parse_response(message, config)
            # 原生 input 是对象；统一调用中仍保留模型流里实际给出的参数字符串。
            raw_by_id = {blocks[index]["id"]: "".join(parts) for index, parts in tool_json.items() if parts}
            for call in result.tool_calls:
                call.arguments = raw_by_id.get(call.call_id, call.arguments)
            return result
    raise StreamInterruptedError("Messages 流在 message_stop 前断开；已显示文本不能作为完整结果。")
