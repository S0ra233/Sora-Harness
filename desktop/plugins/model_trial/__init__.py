"""模型试运行插件：聊天与总结输入，共用平台的模型调用能力。"""
from typing import Any

from ...llm.models import ModelRequest, ModelResponse
from ...llm.messages import ImageContent, content_from_dict, user_message
from ...llm.validation import parse_max_output_tokens, parse_temperature
from ...runtime.contracts import ExecutionContext, Plugin


async def chat(context: ExecutionContext, payload: dict[str, Any]) -> ModelResponse:
    """把本插件的输入组装成模型请求，不直接操作 HTTP 或密钥。"""
    messages = []
    if payload.get("system"):
        messages.append({"role": "system", "content": payload["system"]})
    images = [content_from_dict(part) for part in payload.get("images", [])]
    if any(not isinstance(part, ImageContent) for part in images):
        raise ValueError("试运行 images 仅接收本地图片引用。")
    messages.append(user_message(payload["prompt"], images))
    request = ModelRequest(
        messages=messages, temperature=payload.get("temperature"),
        max_output_tokens=payload.get("max_output_tokens"),
        reasoning_effort=payload.get("reasoning_effort"),
    )
    return await context.complete(request)


async def summarize(context: ExecutionContext, payload: dict[str, Any]) -> ModelResponse:
    """增加总结要求，再复用聊天动作完成模型调用。"""
    # 使用新字典，保留原始输入和系统提示词、温度、输出上限。
    summary_payload = {
        **payload,
        "prompt": f"请概括以下内容，保留主要信息：\n\n{payload['prompt']}",
    }
    return await chat(context, summary_payload)


async def inspect_request(context: ExecutionContext, payload: dict[str, Any]) -> ModelResponse:
    """发送统一请求以检查协议数据往返；返回调用描述，不执行工具。"""
    try:
        request = ModelRequest(**payload)
    except TypeError as exc:
        raise ValueError("请求文件字段应为 messages、tools、temperature、max_output_tokens、reasoning_effort。") from exc
    request.temperature = parse_temperature(request.temperature)
    request.max_output_tokens = parse_max_output_tokens(request.max_output_tokens)
    return await context.complete(request)


def create_plugin() -> Plugin:
    """声明本插件提供哪些动作；保存函数本身，不在这里调用。"""
    return Plugin(plugin_id="model_trial", actions={"chat": chat, "summarize": summarize, "inspect_request": inspect_request})
