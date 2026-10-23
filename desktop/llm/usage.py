"""按协议解释已返回的用量；不估价，不将缺失字段当作零。"""
from collections.abc import Iterable
from dataclasses import dataclass, fields
from typing import Any


@dataclass(frozen=True)
class TokenUsage:
    """输入含缓存、输出含推理；缓存与推理是子集，不再叠加到 total。"""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_miss_tokens: int | None = None


def _number(data: dict[str, Any], key: str) -> int | None:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _sum_known(values: Iterable[int | None]) -> int | None:
    values = list(values)
    return sum(values) if values and all(value is not None for value in values) else None


def normalize_usage(
    raw: dict[str, Any] | None, api_style: str | None, provider_kind: str = "compatible",
) -> TokenUsage | None:
    """协议决定基本语义，提供商决定别名；缺失与零仍严格区分。"""
    if raw is None:
        return None
    cache_read = cache_write = cache_miss = reasoning = total = None
    if api_style in ("chat_completions", "responses"):
        chat = api_style == "chat_completions"
        input_tokens = _number(raw, "prompt_tokens" if chat else "input_tokens")
        output_tokens = _number(raw, "completion_tokens" if chat else "output_tokens")
        input_details = raw.get("prompt_tokens_details" if chat else "input_tokens_details")
        output_details = raw.get("completion_tokens_details" if chat else "output_tokens_details")
        if isinstance(input_details, dict):
            cache_read = _number(input_details, "cached_tokens")
            cache_write = _number(input_details, "cache_write_tokens")
        if isinstance(output_details, dict):
            reasoning = _number(output_details, "reasoning_tokens")
        total = _number(raw, "total_tokens")
        if chat and provider_kind == "deepseek":
            hit = _number(raw, "prompt_cache_hit_tokens")
            if hit is not None:
                cache_read = hit
            cache_miss = _number(raw, "prompt_cache_miss_tokens")
        elif chat and provider_kind == "moonshot" and cache_read is None:
            # 顶层与嵌套字段是同一统计的别名，不能相加。
            cache_read = _number(raw, "cached_tokens")
    elif api_style == "anthropic_messages":
        cache_read = _number(raw, "cache_read_input_tokens")
        cache_write = _number(raw, "cache_creation_input_tokens")
        # Anthropic 的 input_tokens 是未缓存部分，不能直接等同其他协议的输入总量。
        input_tokens = _sum_known((_number(raw, "input_tokens"), cache_read, cache_write))
        output_tokens = _number(raw, "output_tokens")
    elif api_style == "gemini":
        input_tokens = _number(raw, "promptTokenCount")
        cache_read = _number(raw, "cachedContentTokenCount")
        reasoning = _number(raw, "thoughtsTokenCount")
        total = _number(raw, "totalTokenCount")
        output_tokens = _sum_known((_number(raw, "candidatesTokenCount"), reasoning))
        if output_tokens is None and total is not None and input_tokens is not None and total >= input_tokens:
            output_tokens = total - input_tokens
    else:
        return None
    if total is None:
        total = _sum_known((input_tokens, output_tokens))
    usage = TokenUsage(input_tokens, output_tokens, total, cache_read, cache_write, reasoning, cache_miss)
    return usage if any(getattr(usage, item.name) is not None for item in fields(TokenUsage)) else None


def summarize_usage(usages: Iterable[TokenUsage | None], *, unreported_attempts: int = 0) -> dict[str, int | None]:
    """每字段只有全步骤已知才给总数；单步原值可查看，不伪造全程消耗。"""
    usages = list(usages)
    summary = {
        "model_steps": len(usages),
        "reported_steps": sum(usage is not None for usage in usages),
        "unreported_attempts": unreported_attempts,
    }
    for item in fields(TokenUsage):
        summary[item.name] = None if unreported_attempts else _sum_known(
            getattr(usage, item.name) if usage is not None else None for usage in usages
        )
    return summary
