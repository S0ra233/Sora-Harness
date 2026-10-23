"""Gemini Generate Content 消息与工具协议；保留调用所在的签名内容块。"""
import json
import uuid
from functools import partial
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx
from ...attachments.store import ImageStore

from ..contracts import TextSink, ReasoningSink
from ..diagnostics import RequestTrace
from ..reasoning_output import ReasoningDelta
from ..attempts import ModelAttemptSink
from ..models import ModelConfig, ModelRequest, ModelResponse
from ..validation import resolve_model_config
from ..history_projection import consume_projected_request
from .http import ProviderError, model_response, parsed_tool_call, post_json, read_stream
from .protocol_messages import gemini_contents
from .reasoning import reasoning_fields, reasoning_attempts
from .image_input import prepare_request_images
from .stream_events import StreamFormatError, StreamInterruptedError, iter_event_data, parse_event_data, mark_stream_output


class GeminiProvider:
    supports_reasoning_events = True
    supports_request_trace = True
    def __init__(self, config: ModelConfig, api_key: str, *, client: httpx.AsyncClient | None = None,
                 attachments: ImageStore | None = None):
        self.config = resolve_model_config(config)
        self._api_key = api_key
        self._client = client
        self._attachments = attachments

    def _url(self, *, stream: bool) -> str:
        # 接受裸模型名和官方 models/ 前缀，避免生成重复的 models/models/。
        model = self.config.model.lstrip("/").removeprefix("models/")
        address = urlsplit(self.config.endpoint_url.replace("{model}", quote(model, safe="")))
        method = "streamGenerateContent" if stream else "generateContent"
        path = address.path.rsplit(":", 1)[0] + ":" + method
        query = [(key, value) for key, value in parse_qsl(address.query, keep_blank_values=True) if key != "alt"]
        if stream:
            query.append(("alt", "sse"))
        return urlunsplit((address.scheme, address.netloc, path, urlencode(query), ""))

    async def complete(self, request: ModelRequest, *, on_attempt: ModelAttemptSink | None = None,
                       on_reasoning: ReasoningSink | None = None, trace: RequestTrace | None = None) -> ModelResponse:
        data = await post_json(
            self._url(stream=False), {"x-goog-api-key": self._api_key},
            await self._request_payload(request), self.config.timeout_seconds, client=self._client,
            connect_timeout=self.config.connect_timeout_seconds, max_attempts=self.config.max_attempts,
            on_attempt=reasoning_attempts(self.config, request, on_attempt, on_reasoning),
            trace=trace,
        )
        return _parse_response(data, self.config)

    async def stream_complete(
        self, request: ModelRequest, on_text: TextSink, *, on_attempt: ModelAttemptSink | None = None,
        on_reasoning: ReasoningSink | None = None,
        trace: RequestTrace | None = None,
    ) -> ModelResponse:
        return await read_stream(
            self._url(stream=True), {"x-goog-api-key": self._api_key},
            await self._request_payload(request), self.config.timeout_seconds,
            on_text, partial(_read_stream, config=self.config, on_reasoning=on_reasoning), client=self._client,
            connect_timeout=self.config.connect_timeout_seconds, max_attempts=self.config.max_attempts,
            on_attempt=reasoning_attempts(self.config, request, on_attempt, on_reasoning),
            trace=trace,
        )

    async def _request_payload(self, request: ModelRequest) -> dict[str, Any]:
        request, _ = consume_projected_request(request, self.config)
        reasoning = reasoning_fields(self.config, request)
        images = await prepare_request_images(request, self.config, self._attachments)
        systems, turns = gemini_contents(request.messages, self.config, images, projected=True)
        payload: dict[str, Any] = {"contents": turns}
        if request.tools:
            payload["tools"] = [{"functionDeclarations": [
                {"name": tool.name, "description": tool.description, "parametersJsonSchema": tool.parameters}
                for tool in request.tools
            ]}]
        if systems:
            payload["systemInstruction"] = {"parts": [{"text": text} for text in systems]}
        if request.tool_choice == "none" and request.tools:
            payload["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
        generation = dict(reasoning)
        if request.temperature is not None:
            generation["temperature"] = request.temperature
        if request.max_output_tokens is not None:
            generation["maxOutputTokens"] = request.max_output_tokens
        if generation:
            payload["generationConfig"] = generation
        return payload


def _candidate(data: dict[str, Any]) -> dict[str, Any]:
    feedback = data.get("promptFeedback") or {}
    if not isinstance(feedback, dict):
        raise ProviderError("Gemini 的 promptFeedback 不是对象。")
    if feedback.get("blockReason") not in (None, "BLOCK_REASON_UNSPECIFIED"):
        raise ProviderError("Gemini 拒绝了这次输入，请查看所选服务的输入限制。")
    candidates = data.get("candidates", [])
    if not isinstance(candidates, list) or (candidates and not isinstance(candidates[0], dict)):
        raise ProviderError("Gemini 的 candidates 不是有效数组。")
    return candidates[0] if candidates else {}


def _parts(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    content = candidate.get("content") or {}
    if not isinstance(content, dict) or not isinstance(content.get("parts", []), list):
        raise ProviderError("Gemini 的 content.parts 不是有效数组。")
    parts = content.get("parts", [])
    for part in parts:
        if not isinstance(part, dict):
            raise ProviderError("Gemini 内容片段不是对象。")
        if any(key in part for key in ("functionResponse", "inlineData", "fileData", "executableCode", "codeExecutionResult")):
            raise ProviderError("当前只支持 Gemini 文本、推理签名与 functionCall，不支持其他输出。")
        if "text" in part and not isinstance(part["text"], str):
            raise ProviderError("Gemini 文本片段不是字符串。")
    return parts


def _parse_response(data: dict[str, Any], config: ModelConfig) -> ModelResponse:
    candidate = _candidate(data)
    parts = _parts(candidate)
    text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
    calls = []
    native_ids = {}
    for part in parts:
        if "functionCall" not in part:
            continue
        function = part["functionCall"]
        if not isinstance(function, dict) or not isinstance(function.get("args", {}), dict):
            raise ProviderError("Gemini 的 functionCall 或 args 不是对象。")
        native_id = function.get("id")
        # Generate Content 的旧模型不返回 id；本地 ID 只用于关联，不伪造服务端字段。
        call_id = native_id if native_id is not None else "local_" + uuid.uuid4().hex
        calls.append(parsed_tool_call(call_id, function.get("name"), json.dumps(function.get("args", {}), ensure_ascii=False)))
        native_ids[call_id] = native_id
    reason = candidate.get("finishReason")
    if reason in ("MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL", "TOO_MANY_TOOL_CALLS", "MISSING_THOUGHT_SIGNATURE", "MALFORMED_RESPONSE"):
        raise ProviderError("Gemini 返回了无效工具调用、缺失签名或其他失败状态。")
    status = "completed" if reason == "STOP" else "unknown" if reason in (None, "FINISH_REASON_UNSPECIFIED") else "incomplete"
    return model_response(
        text, data.get("usageMetadata"), reason, status, data,
        tool_calls=calls, config=config,
        replay={"content": {"role": "model", "parts": parts}, "call_ids": native_ids},
    )


async def _read_stream(response: httpx.Response, on_text: TextSink, *, config: ModelConfig,
                       on_reasoning: ReasoningSink | None = None) -> ModelResponse:
    native: dict[str, Any] = {}
    candidate: dict[str, Any] = {}
    parts: list[dict[str, Any]] = []
    call_indexes: dict[str, int] = {}
    thought_group, previous_thought = 0, False
    async for event_data in iter_event_data(response):
        event = parse_event_data(event_data)
        current = _candidate(event)
        current_parts = _parts(current)
        # 保留思考标记与 thoughtSignature，UI 只显示普通文本部分。
        _append_parts(parts, call_indexes, current_parts)
        for part in current_parts:
            if part.get("text") or part.get("thoughtSignature") or "functionCall" in part:
                mark_stream_output()
            thought = part.get("thought") is True
            if previous_thought and not thought:
                if on_reasoning is not None:
                    on_reasoning(ReasoningDelta(f"gemini:{thought_group}", status="finished", kind="summary"))
                thought_group += 1
            previous_thought = thought
            if not part.get("thought") and part.get("text"):
                on_text(part["text"])
            elif part.get("thought") and on_reasoning is not None:
                on_reasoning(ReasoningDelta(f"gemini:{thought_group}", part.get("text", ""), kind="summary"))
        content = {**(candidate.get("content") or {}), **(current.get("content") or {})}
        candidate.update(current)
        candidate["content"] = {**content, "parts": parts}
        # usageMetadata 是累计统计；新的对象替换旧统计，不逐次相加。
        native.update(event)
    if not candidate.get("finishReason") or candidate["finishReason"] == "FINISH_REASON_UNSPECIFIED":
        raise StreamInterruptedError("Gemini 流缺少 finishReason；已显示文本不能作为完整结果。")
    native["candidates"] = [candidate]
    return _parse_response(native, config)


def _append_parts(parts: list[dict[str, Any]], call_indexes: dict[str, int], incoming: list[dict[str, Any]]) -> None:
    """标准流式响应的调用按原生 ID 合并；没有 ID 的并行同名调用不能猜测合并。"""
    for part in incoming:
        function = part.get("functionCall")
        if function is not None and not isinstance(function, dict):
            raise StreamFormatError("Gemini 流式 functionCall 不是对象。")
        call_id = function.get("id") if function is not None else None
        if call_id is not None and (not isinstance(call_id, str) or not call_id):
            raise StreamFormatError("Gemini 流式工具 ID 无效。")
        if call_id is not None and call_id in call_indexes:
            index = call_indexes[call_id]
            previous = parts[index]
            parts[index] = {**previous, **part, "functionCall": {**previous["functionCall"], **function}}
        else:
            if call_id is not None:
                call_indexes[call_id] = len(parts)
            parts.append(part)
