"""统一消息到各 API 的转换；原生续接先经过目标历史投影。"""
import json
from copy import deepcopy
from typing import Any

from ..messages import ImageContent, Message, ModelMessage, TextContent, ToolCall, ToolResult
from ...attachments.store import EncodedImage
from ..models import ModelConfig
from ..protocols import PROTOCOLS
from .http import ProviderError
from ..history_projection import project_messages, same_origin


def replay_payload(message: ModelMessage, config: ModelConfig) -> dict[str, Any] | None:
    state = message.replay_state
    if state is None:
        return None
    if not same_origin(state, config):
        raise ProviderError("原生续接来源与目标不一致，须先构造目标历史视图。")
    expected = PROTOCOLS[config.api_style].replay_fields
    if any(not isinstance(state.payload.get(key), kind) for key, kind in expected):
        raise ProviderError("请求中的原生续接数据结构不完整；请使用 as_message() 的完整结果。")
    return deepcopy(state.payload)


def object_arguments(call: ToolCall) -> dict[str, Any]:
    """部分协议要求对象参数；这里只转换历史消息，不校验或执行工具。"""
    try:
        value = json.loads(call.arguments)
    except ValueError as exc:
        raise ProviderError("历史工具调用参数不是完整 JSON，不能续接。") from exc
    if not isinstance(value, dict):
        raise ProviderError("历史工具调用参数必须是 JSON 对象。")
    return value


def result_text(result: ToolResult) -> str:
    return result.content


def _content_blocks(
    message: ModelMessage, style: str, images: dict[str, EncodedImage],
) -> list[dict[str, Any]]:
    """图片只在这里变成厂商内容块；Base64 不进入统一消息或日志。"""
    blocks = []
    for part in message.content:
        if isinstance(part, TextContent):
            if not part.text:
                # 图片单独发送时不创建空 text 块，Messages 协议会拒绝它。
                continue
            blocks.append({"text": part.text} if style == "gemini" else {
                "type": "input_text" if style == "responses" else "text", "text": part.text,
            })
            continue
        image = images.get(part.attachment_id)
        if image is None:
            raise ProviderError("图片尚未经过请求准备，不能静默移除附件。")
        if style == "chat_completions":
            blocks.append({"type": "image_url", "image_url": {"url": image.data_url}})
        elif style == "responses":
            blocks.append({"type": "input_image", "image_url": image.data_url})
        elif style == "anthropic_messages":
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": image.mime_type, "data": image.data,
            }})
        else:
            blocks.append({"inlineData": {"mimeType": image.mime_type, "data": image.data}})
    return blocks


def chat_messages(
    messages: list[Message], config: ModelConfig, images: dict[str, EncodedImage] | None = None, *, projected: bool = False,
) -> list[dict[str, Any]]:
    messages = messages if projected else project_messages(messages, config)
    output = []
    for message in messages:
        if isinstance(message, ToolResult):
            output.append({"role": "tool", "tool_call_id": message.call_id, "content": result_text(message)})
            continue
        replay = replay_payload(message, config)
        if replay is not None:
            output.append(replay["message"])
            continue
        if message.role == "assistant" and not message.content and not message.tool_calls:
            continue
        item: dict[str, Any] = {"role": message.role, "content": message.text}
        if any(isinstance(part, ImageContent) for part in message.content):
            item["content"] = _content_blocks(message, "chat_completions", images or {})
        if message.tool_calls:
            item["tool_calls"] = [
                {"id": call.call_id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}
                for call in message.tool_calls
            ]
        output.append(item)
    return output


def responses_input(
    messages: list[Message], config: ModelConfig, images: dict[str, EncodedImage] | None = None, *, projected: bool = False,
) -> list[dict[str, Any]]:
    messages = messages if projected else project_messages(messages, config)
    output = []
    for message in messages:
        if isinstance(message, ToolResult):
            output.append({"type": "function_call_output", "call_id": message.call_id, "output": result_text(message)})
            continue
        replay = replay_payload(message, config)
        if replay is not None:
            # 包含 reasoning 的原顺序输出，不能只留下 function_call。
            output.extend(replay["output"])
            continue
        if message.role == "assistant" and not message.content and not message.tool_calls:
            continue
        if message.text or not message.tool_calls:
            output.append({"role": message.role, "content": message.text})
            if any(isinstance(part, ImageContent) for part in message.content):
                output[-1]["content"] = _content_blocks(message, "responses", images or {})
        output.extend(
            {"type": "function_call", "call_id": call.call_id, "name": call.name, "arguments": call.arguments}
            for call in message.tool_calls
        )
    return output


def _append_turn(turns: list[dict[str, Any]], role: str, field: str, blocks: list[dict[str, Any]]) -> None:
    """合并同角色相邻消息，保证并行工具结果在同一轮用户内容中返回。"""
    if turns and turns[-1]["role"] == role:
        turns[-1][field].extend(blocks)
    else:
        turns.append({"role": role, field: blocks})


def anthropic_messages(
    messages: list[Message], config: ModelConfig, images: dict[str, EncodedImage] | None = None, *, projected: bool = False,
) -> tuple[list[str], list[dict[str, Any]]]:
    messages = messages if projected else project_messages(messages, config)
    systems, turns = [], []
    for message in messages:
        if isinstance(message, ToolResult):
            blocks = [{"type": "tool_result", "tool_use_id": message.call_id, "content": message.content, "is_error": message.is_error}]
            _append_turn(turns, "user", "content", blocks)
            continue
        if message.role == "system":
            systems.append(message.text)
            continue
        replay = replay_payload(message, config)
        blocks = replay["content"] if replay is not None else _content_blocks(message, "anthropic_messages", images or {})
        if replay is None:
            blocks.extend(
                {"type": "tool_use", "id": call.call_id, "name": call.name, "input": object_arguments(call)}
                for call in message.tool_calls
            )
        if blocks:
            _append_turn(turns, message.role, "content", blocks)
    return systems, turns


def _gemini_result(result: ToolResult, native_id: str | None) -> dict[str, Any]:
    try:
        body = json.loads(result.content)
    except ValueError:
        body = {"error" if result.is_error else "result": result.content}
    if not isinstance(body, dict):
        body = {"result": body}
    response = {"name": result.name, "response": body}
    if native_id is not None:
        response["id"] = native_id
    return {"functionResponse": response}


def gemini_contents(
    messages: list[Message], config: ModelConfig, images: dict[str, EncodedImage] | None = None, *, projected: bool = False,
) -> tuple[list[str], list[dict[str, Any]]]:
    messages = messages if projected else project_messages(messages, config)
    systems, turns = [], []
    native_ids: dict[str, str | None] = {}
    for message in messages:
        if isinstance(message, ToolResult):
            _append_turn(turns, "user", "parts", [_gemini_result(message, native_ids[message.call_id])])
            continue
        if message.role == "system":
            systems.append(message.text)
            continue
        replay = replay_payload(message, config)
        if replay is not None:
            if not isinstance(replay["content"].get("parts"), list):
                raise ProviderError("Gemini 续接数据缺少原始 parts 数组。")
            parts = replay["content"]["parts"]
            if any(call.call_id not in replay["call_ids"] for call in message.tool_calls):
                raise ProviderError("Gemini 续接数据缺少调用 ID 的关联，请保留完整 assistant_message。")
            native_ids.update(replay["call_ids"])
        else:
            parts = _content_blocks(message, "gemini", images or {})
            for call in message.tool_calls:
                parts.append({"functionCall": {"id": call.call_id, "name": call.name, "args": object_arguments(call)}})
                native_ids[call.call_id] = call.call_id
        if parts:
            _append_turn(turns, "model" if message.role == "assistant" else "user", "parts", parts)
    return systems, turns
