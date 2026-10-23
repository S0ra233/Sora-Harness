"""通过 Chat Completions 协议发起完整或流式消息与工具请求。"""
from functools import partial
from typing import Any

import httpx
from ....attachments.store import ImageStore

from ...contracts import TextSink, ReasoningSink
from ...diagnostics import RequestTrace
from ...attempts import ModelAttemptSink
from ...models import ModelConfig, ModelRequest, ModelResponse
from ...validation import resolve_model_config
from ...history_projection import consume_projected_request
from ..image_input import prepare_request_images
from ..http import post_json, read_stream
from ..protocol_messages import chat_messages
from ..reasoning import reasoning_fields, reasoning_attempts
from .response import parse_chat_response as _parse_response
from .stream import read_chat_stream


class HttpChatProvider:
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
        """读取 SSE 内容；调用方取消时退出客户端和响应上下文。"""
        return await read_stream(
            self.config.endpoint_url, {"Authorization": f"Bearer {self._api_key}"},
            await self._request_payload(request, stream=True), self.config.timeout_seconds,
            on_text, partial(read_chat_stream, config=self.config, on_reasoning=on_reasoning), client=self._client,
            connect_timeout=self.config.connect_timeout_seconds, max_attempts=self.config.max_attempts,
            on_attempt=reasoning_attempts(self.config, request, on_attempt, on_reasoning),
            trace=trace,
        )

    async def _request_payload(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        """附件异步准备，厂商参数仅在已确认的服务规则下映射。"""
        request, _ = consume_projected_request(request, self.config)
        reasoning = reasoning_fields(self.config, request)
        images = await prepare_request_images(request, self.config, self._attachments)
        payload = {"model": self.config.model, "messages": chat_messages(request.messages, self.config, images, projected=True), "stream": stream}
        if stream and self.config.provider_rules.stream_usage:
            payload["stream_options"] = {"include_usage": True}
        if request.tools:
            payload["tools"] = [
                {"type": "function", "function": {
                    "name": tool.name, "description": tool.description, "parameters": tool.parameters, "strict": False,
                }} for tool in request.tools
            ]
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.tool_choice is not None and request.tools:
            payload["tool_choice"] = request.tool_choice
        if request.max_output_tokens is not None:
            payload[self.config.provider_rules.output_limit_field] = request.max_output_tokens
        payload.update(reasoning)
        return payload
