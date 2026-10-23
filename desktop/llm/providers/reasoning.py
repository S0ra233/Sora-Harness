"""已选择的 effort 到原生请求字段；配置不能提供任意 API JSON。"""
from typing import Any

from ..models import ModelConfig, ModelRequest
from ..reasoning import resolve_effort
from ..reasoning_output import ReasoningDelta


def reasoning_fields(config: ModelConfig, request: ModelRequest) -> dict[str, Any]:
    """Gemini 返回 generationConfig 内的字段，其余协议返回请求顶层字段。"""
    value = resolve_effort(
        config.reasoning, request.reasoning_effort, has_tools=bool(request.tools) and request.tool_choice != "none",
        temperature=request.temperature, max_output_tokens=request.max_output_tokens,
    )
    if value is None:
        return {}
    profile = config.reasoning
    adapter, style = profile.adapter, config.api_style
    if adapter == "openai":
        return {"reasoning_effort": value} if style == "chat_completions" else {"reasoning": {"effort": value}}
    if adapter == "deepseek":
        if style == "responses":
            return {"reasoning": {"effort": value}}
        fields = {"thinking": {"type": "disabled" if value == "none" else "enabled"}}
        if value != "none":
            fields.update({"reasoning_effort": value} if style == "chat_completions" else {"output_config": {"effort": value}})
        return fields
    if adapter == "anthropic":
        return {"output_config": {"effort": value}}
    if adapter == "anthropic_adaptive":
        if value == "none":
            return {"thinking": {"type": "disabled"}}
        return {"thinking": {"type": "adaptive"}, "output_config": {"effort": value}}
    if adapter == "anthropic_budget":
        if value == 0:
            return {"thinking": {"type": "disabled"}}
        return {"thinking": {"type": "enabled", "budget_tokens": value}}
    if adapter == "gemini_level":
        return {"thinkingConfig": {"thinkingLevel": value}}
    if adapter == "gemini_budget":
        return {"thinkingConfig": {"thinkingBudget": value}}
    raise ValueError("此模型没有可执行的推理参数映射。")


def reasoning_attempts(config, request, on_attempt, on_reasoning):
    """先关联 HTTP 尝试，再通知明确的思考模式；没有证据时不显示思考。"""
    if on_reasoning is None:
        return on_attempt
    fields = reasoning_fields(config, request)
    thinking = fields.get("thinking", {})
    enabled = config.reasoning.default_thinking if config.reasoning is not None else None
    if thinking.get("type") in {"enabled", "adaptive"}:
        enabled = True
    elif thinking.get("type") == "disabled":
        enabled = False
    elif "reasoning_effort" in fields or "reasoning" in fields:
        enabled = (fields.get("reasoning_effort") or fields.get("reasoning", {}).get("effort")) != "none"
    elif "thinkingConfig" in fields:
        value = fields["thinkingConfig"]
        enabled = False if value.get("thinkingBudget") == 0 else True if "thinkingLevel" in value or value.get("thinkingBudget", -1) > 0 else None
    def accept(event):
        if on_attempt is not None:
            on_attempt(event)
        if event.phase == "started" and enabled is True:
            on_reasoning(ReasoningDelta("pending", status="started"))
    return accept
