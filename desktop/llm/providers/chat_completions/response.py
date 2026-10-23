"""Chat Completions 普通响应与流式组装结果共用的解析。"""
from typing import Any

from ...models import ModelConfig, ModelResponse
from ..http import ProviderError, model_response, parsed_tool_call


def parse_chat_response(data: dict[str, Any], config: ModelConfig) -> ModelResponse:
    try:
        choice = data["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError("响应缺少 choices[0].message，请确认接口使用 Chat Completions 协议") from exc
    if not isinstance(message, dict):
        raise ProviderError("响应中的 message 不是对象")
    reason = choice.get("finish_reason")
    if message.get("function_call") or reason == "function_call":
        raise ProviderError("旧版 function_call 尚未支持，请使用 tools/tool_calls 协议。")
    if message.get("refusal"):
        raise ProviderError("模型拒绝了这次请求")
    native_calls = message.get("tool_calls") or []
    if not isinstance(native_calls, list):
        raise ProviderError("Chat 的 tool_calls 不是数组。")
    calls = []
    for item in native_calls:
        if not isinstance(item, dict) or item.get("type") != "function" or not isinstance(item.get("function"), dict):
            raise ProviderError("当前仅支持 function 类型工具调用。")
        function = item["function"]
        calls.append(parsed_tool_call(item.get("id"), function.get("name"), function.get("arguments")))
    if reason == "tool_calls" and not calls:
        raise ProviderError("Chat 宣告工具调用结束，但未返回调用内容。")
    text = message.get("content")
    if text is None:
        text = ""
    status = "incomplete" if reason in ("length", "content_filter") else "completed" if reason in ("stop", "tool_calls") else "unknown"
    # 只回传可作为 assistant 输入的字段；保留兼容模型所需的 reasoning_content。
    replay_message = {"role": "assistant", "content": text}
    for field in ("reasoning_content", "reasoning"):
        if message.get(field) is not None and not isinstance(message[field], str):
            raise ProviderError("Chat 推理文本字段不是字符串。")
    details = message.get("reasoning_details")
    if details is not None and (not isinstance(details, list) or any(not isinstance(item, dict) for item in details)):
        raise ProviderError("Chat reasoning_details 不是对象数组。")
    for field in ("tool_calls", "reasoning_content", "reasoning", "reasoning_details"):
        if field in message:
            replay_message[field] = message[field]
    return model_response(
        text, data.get("usage"), reason, status, data,
        tool_calls=calls, config=config, replay={"message": replay_message},
    )
