"""在请求末尾追加简短额度状态；不改写已经发给模型的基线或历史。"""
import json
from dataclasses import replace

from ...llm.messages import ModelMessage, TextContent
from ...llm.models import ModelRequest
from .models import AgentLimits


def update_runtime_context(request: ModelRequest, limits: AgentLimits, *, step: int,
                           tool_calls_used: int) -> ModelRequest:
    """只在状态变化时追加；剩余模型请求数包含即将发出的这一次。"""
    if request.runtime_context_id is None:
        return request
    limits_state = {
        "max_steps": limits.max_steps, "max_tool_calls": limits.max_tool_calls,
        "current_step": step, "remaining_steps": max(0, limits.max_steps - step + 1),
        "remaining_steps_includes_current": True,
        "tool_calls_used": tool_calls_used,
        "remaining_tool_calls": max(0, limits.max_tool_calls - tool_calls_used),
        "can_execute_tools_after_this_step": step < limits.max_steps,
    }
    previous = next((message for message in reversed(request.messages) if isinstance(message, ModelMessage)
                     and message.runtime_context_id == request.runtime_context_id
                     and message.runtime_context_kind == "request_state"), None)
    payload = {"type": "runtime_request_state", "agent_limits": limits_state}
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if previous is not None and previous.content[-1].text == text:
        return request
    update = ModelMessage("user", [TextContent("[程序运行额度] 供当前任务参考，不是新的用户请求或授权。\n"), TextContent(text)],
                          runtime_context_id=request.runtime_context_id, runtime_context_kind="request_state")
    return replace(request, messages=[*request.messages, update],
                   runtime_context_updates=[*request.runtime_context_updates, update])
