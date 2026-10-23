"""Responses 消息与工具协议；输出项保留原结构用于同后端续接。"""
from functools import partial
from typing import Any

import httpx
from ...attachments.store import ImageStore

from ..contracts import TextSink, ReasoningSink
from ..diagnostics import RequestTrace
from ..reasoning_output import ReasoningDelta, responses_reasoning_blocks, responses_reasoning_id
from ..attempts import ModelAttemptSink
from ..models import ModelConfig, ModelRequest, ModelResponse
from ..validation import resolve_model_config
from ..history_projection import consume_projected_request
from .http import ProviderError, model_response, parsed_tool_call, post_json, read_stream
from .protocol_messages import responses_input
from .reasoning import reasoning_fields, reasoning_attempts
from .image_input import prepare_request_images
from .stream_events import StreamFormatError, StreamInterruptedError, iter_event_data, parse_event_data, mark_stream_output


class ResponsesProvider:
    supports_reasoning_events = True
    supports_request_trace = True
    def __init__(self, config: ModelConfig, api_key: str, *, client: httpx.AsyncClient | None = None,
                 attachments: ImageStore | None = None):
        self.config = resolve_model_config(config)
        self._api_key = api_key
        self._client = client
        self._attachments = attachments

    async def complete(self, request: ModelRequest, *, on_attempt: ModelAttemptSink | None = None,
                       on_reasoning: ReasoningSink | None = None, trace: RequestTrace | None = None) -> ModelResponse:
        data = await post_json(
            self.config.endpoint_url, {"Authorization": f"Bearer {self._api_key}"},
            await self._request_payload(request, stream=False), self.config.timeout_seconds, client=self._client,
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
            self.config.endpoint_url, {"Authorization": f"Bearer {self._api_key}"},
            await self._request_payload(request, stream=True), self.config.timeout_seconds,
            on_text, partial(_read_stream, config=self.config, on_reasoning=on_reasoning), client=self._client,
            connect_timeout=self.config.connect_timeout_seconds, max_attempts=self.config.max_attempts,
            on_attempt=reasoning_attempts(self.config, request, on_attempt, on_reasoning),
            trace=trace,
        )

    async def _request_payload(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        request, _ = consume_projected_request(request, self.config)
        reasoning = reasoning_fields(self.config, request)
        images = await prepare_request_images(request, self.config, self._attachments)
        # 本轮请求由本地组装，不依赖服务端自动保存的会话状态。
        payload = {
            "model": self.config.model, "input": responses_input(request.messages, self.config, images, projected=True),
            "stream": stream, "store": False,
        }
        if self.config.provider_rules.include_encrypted_reasoning:
            # 支持加密推理项的服务显式请求；原生输出仍由协议解析和回传。
            payload["include"] = ["reasoning.encrypted_content"]
        if request.tools:
            payload["tools"] = [
                {"type": "function", "name": tool.name, "description": tool.description,
                 "parameters": tool.parameters, "strict": False}
                for tool in request.tools
            ]
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tool_choice is not None and request.tools:
            payload["tool_choice"] = request.tool_choice
        if request.max_output_tokens is not None:
            payload["max_output_tokens"] = request.max_output_tokens
        payload.update(reasoning)
        return payload


def _parse_response(data: dict[str, Any], config: ModelConfig) -> ModelResponse:
    status = data.get("status")
    if data.get("error") or status not in ("completed", "incomplete"):
        raise ProviderError("Responses 未返回 completed 或 incomplete 结果，请检查服务响应。")
    output = data.get("output")
    if not isinstance(output, list):
        raise ProviderError("Responses 响应缺少 output 数组。")
    parts = []
    calls = []
    for item in output:
        if not isinstance(item, dict):
            raise ProviderError("Responses 输出项不是对象。")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") == "function_call":
            if item.get("status") not in (None, "completed"):
                raise ProviderError("Responses 的工具调用尚未完整结束。")
            calls.append(parsed_tool_call(item.get("call_id"), item.get("name"), item.get("arguments")))
            continue
        if item.get("type") != "message":
            raise ProviderError("当前仅支持 Responses 文本、推理与 function_call，不支持服务端或其他工具类型。")
        content = item.get("content")
        if not isinstance(content, list):
            raise ProviderError("Responses 消息缺少 content 数组。")
        for block in content:
            if not isinstance(block, dict):
                raise ProviderError("Responses 内容块不是对象。")
            if block.get("type") == "refusal":
                raise ProviderError("模型拒绝了这次请求。")
            if block.get("type") != "output_text" or not isinstance(block.get("text"), str):
                raise ProviderError("当前阶段只支持 Responses 的 output_text 内容。")
            parts.append(block["text"])
    details = data.get("incomplete_details") or {}
    if not isinstance(details, dict):
        raise ProviderError("Responses 的 incomplete_details 不是对象。")
    reason = details.get("reason") if status == "incomplete" else "completed"
    return model_response(
        "".join(parts), data.get("usage"), reason, status, data,
        tool_calls=calls, config=config, replay={"output": output},
    )


class _ReasoningStream:
    """一次HTTP尝试内的公开块状态；文字由既有通知接收方累计，不另存全文。"""

    def __init__(self, sink: ReasoningSink | None) -> None:
        self._sink = sink
        self._items: dict[int, str] = {}
        self._pending: set[str] = set()
        self._blocks: dict[str, tuple[str, str]] = {}
        self._text_seen: set[str] = set()
        self._finished: set[str] = set()

    def _source(self, event: dict) -> tuple[object, int]:
        index = event.get("output_index", 0)
        if type(index) is not int or index < 0:
            raise StreamFormatError("Responses 思考的output_index须为非负整数。")
        identifier = event.get("item_id") or self._items.get(index)
        if isinstance(identifier, str) and identifier:
            self._items[index] = identifier
        return identifier, index

    def _notify(self, identifier: str, *, text: str = "", status: str, kind: str = "reasoning") -> None:
        if self._sink is not None:
            self._sink(ReasoningDelta(identifier, text, status, kind))

    def start_item(self, event: dict, item: dict) -> None:
        _, index = self._source({**event, "item_id": item.get("id")})
        # 等待标记依输出位置绑定；item_id稍后才出现也能准确收尾。
        pending = responses_reasoning_id(None, index, "item")
        self._pending.add(pending)
        self._notify(pending, status="started")

    def _start_block(self, identifier: str, item: str, kind: str) -> None:
        if item in self._pending:
            self._pending.remove(item)
            self._notify(item, status="finished")
        if identifier not in self._blocks:
            self._blocks[identifier] = item, kind
            self._notify(identifier, status="started", kind=kind)

    def text(self, event: dict, kind: str, *, done: bool) -> None:
        identifier, index = self._source(event)
        part_index = event.get("summary_index" if kind == "summary" else "content_index", 0)
        if type(part_index) is not int or part_index < 0:
            raise StreamFormatError("Responses 思考内容索引须为非负整数。")
        text = event.get("text" if done else "delta", "" if done else None)
        if not isinstance(text, str):
            raise StreamFormatError("Responses 公开思考文字不是字符串。")
        block = responses_reasoning_id(identifier, index, kind, part_index)
        item = responses_reasoning_id(None, index, "item")
        self._start_block(block, item, kind)
        # done可能带累计全文；已有delta时不能再次追加。最终response负责权威定稿。
        if text and (not done or block not in self._text_seen):
            mark_stream_output()
            self._text_seen.add(block)
            self._notify(block, text=text, status="delta", kind=kind)
        if done:
            self._finish_block(block)

    def part(self, event: dict, *, done: bool) -> None:
        part = event.get("part")
        if not isinstance(part, dict):
            return
        kind = {"reasoning_text": "reasoning", "summary_text": "summary"}.get(part.get("type"))
        if kind is None:
            return
        # part和text的done可能同时出现，共用已显示标记避免重复全文。
        self.text({**event, "text": part.get("text", ""), "delta": part.get("text", "")}, kind, done=done)

    def _finish_block(self, identifier: str) -> None:
        if identifier not in self._finished:
            self._finished.add(identifier)
            self._notify(identifier, status="finished", kind=self._blocks[identifier][1])

    def finish_item(self, event: dict, item: dict) -> None:
        _, index = self._source({**event, "item_id": item.get("id")})
        item_key = responses_reasoning_id(None, index, "item")
        for block in responses_reasoning_blocks(item, index):
            self._start_block(block.block_id, item_key, block.kind)
            if block.block_id not in self._text_seen:
                if block.text:
                    mark_stream_output()
                self._text_seen.add(block.block_id)
                self._notify(block.block_id, text=block.text, status="delta", kind=block.kind)
        for block_id, (source, _) in self._blocks.items():
            if source == item_key:
                self._finish_block(block_id)
        if item_key in self._pending:
            self._pending.remove(item_key)
            self._notify(item_key, status="finished")

    def finish_response(self, data: dict) -> None:
        output = data.get("output")
        if isinstance(output, list):
            for index, item in enumerate(output):
                if isinstance(item, dict) and item.get("type") == "reasoning":
                    self.finish_item({"output_index": index}, item)
        for block_id in self._blocks:
            self._finish_block(block_id)
        for pending in self._pending:
            self._notify(pending, status="finished")
        self._pending.clear()


def _item_has_text(item: dict) -> bool:
    """只用实际文字判断产出，内容缺省或未开始不算输出。"""
    content = item.get("content")
    return isinstance(content, list) and any(isinstance(block, dict) and block.get("text") for block in content)


async def _read_stream(response: httpx.Response, on_text: TextSink, *, config: ModelConfig,
                       on_reasoning: ReasoningSink | None = None) -> ModelResponse:
    reasoning = _ReasoningStream(on_reasoning)
    async for event_data in iter_event_data(response):
        event = parse_event_data(event_data)
        kind = event.get("type")
        if kind == "response.output_text.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise StreamFormatError("Responses 文本增量不是字符串。")
            if delta:
                on_text(delta)
        elif kind == "response.output_text.done":
            # 有些服务只给累计定稿；它也证明已经产出，不能因未收到delta而整轮重发。
            text = event.get("text")
            if not isinstance(text, str):
                raise StreamFormatError("Responses 文本定稿不是字符串。")
            if text:
                mark_stream_output()
        elif kind in ("response.reasoning_text.delta", "response.reasoning_summary_text.delta"):
            reasoning.text(event, "summary" if kind == "response.reasoning_summary_text.delta" else "reasoning", done=False)
        elif kind in ("response.reasoning_text.done", "response.reasoning_summary_text.done"):
            reasoning.text(event, "summary" if kind == "response.reasoning_summary_text.done" else "reasoning", done=True)
        elif kind in ("response.reasoning_summary_part.added", "response.content_part.added"):
            reasoning.part(event, done=False)
        elif kind in ("response.reasoning_summary_part.done", "response.content_part.done"):
            reasoning.part(event, done=True)
        elif kind in ("response.completed", "response.incomplete"):
            data = event.get("response")
            if not isinstance(data, dict):
                raise StreamFormatError("Responses 结束事件缺少 response 对象。")
            reasoning.finish_response(data)
            # 终止事件包含完整 output，直接解析，避免再次拼接工具参数。
            return _parse_response(data, config)
        elif kind == "response.failed":
            raise StreamFormatError("Responses 服务报告本次生成失败。")
        elif kind == "response.refusal.delta":
            raise StreamFormatError("模型拒绝了这次请求。")
        elif kind == "response.output_item.added":
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") not in ("message", "reasoning", "function_call"):
                raise StreamFormatError("当前不支持 Responses 服务端工具或其他输出项。")
            if item.get("type") == "reasoning":
                reasoning.start_item(event, item)
                if item.get("encrypted_content"):
                    mark_stream_output()
            elif item.get("type") == "function_call":
                mark_stream_output()
            elif _item_has_text(item):
                mark_stream_output()
        elif kind in ("response.function_call_arguments.delta", "response.function_call_arguments.done"):
            value = event.get("delta" if kind.endswith(".delta") else "arguments")
            if not isinstance(value, str):
                raise StreamFormatError("Responses 工具参数增量不是字符串。")
            if value:
                mark_stream_output()
        elif kind == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "reasoning":
                if item.get("encrypted_content"):
                    mark_stream_output()
                reasoning.finish_item(event, item)
            elif isinstance(item, dict) and item.get("type") == "function_call":
                mark_stream_output()
            elif isinstance(item, dict) and item.get("type") == "message" and _item_has_text(item):
                mark_stream_output()
    raise StreamInterruptedError("Responses 流在完成事件前断开；已显示文本不能作为完整结果。")
