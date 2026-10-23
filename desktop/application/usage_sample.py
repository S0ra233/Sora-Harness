"""主模型请求的共同取样：上下文和TPS取最近成功请求，缓存按本轮结算值合计。"""
from dataclasses import dataclass
from math import isfinite

from ..llm.usage import TokenUsage


@dataclass(frozen=True)
class RequestUsage:
    usage: TokenUsage | None
    seconds: float | None
    window: int | None = None


@dataclass(frozen=True)
class UsageSample:
    latest: RequestUsage | None = None
    cache_read: int | None = None
    cache_input: int | None = None
    settled_requests: int = 0
    running: bool = False
    scope: str = "整轮"


def request_seconds(attempts) -> float | None:
    """仅取最终成功尝试；旧记录没有耗时就保持未知，不用整轮时间替代。"""
    event = next((item for item in reversed(attempts) if item.phase == "received"), None)
    value = event.duration_seconds if event else None
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value > 0 else None


def usage_after_step(run_id, started_at, boundary) -> int | None:
    """压缩前运行无资格显示；所属运行仅统计压缩步骤之后的请求。"""
    if boundary is None:
        return 0
    if run_id == boundary.owner_run_id:
        return boundary.after_step
    return 0 if (started_at or "") >= (boundary.finished_at or boundary.started_at) else None


def from_steps(steps, *, running=False, window=None, after_step=0) -> UsageSample:
    if after_step is None:
        return UsageSample(running=running)
    steps = [step for step in steps if step.step > after_step]
    requests = [RequestUsage(step.token_usage, request_seconds(step.attempts), window) for step in steps
                if step.completion_status in {"completed", "requires_tool"}]
    unknown = any(event.phase in {"failed", "cancelled"} for step in steps for event in step.attempts)
    return _fold(requests, unknown=unknown, running=running)


def from_live(attempts, *, running=False, window=None, after_step=0) -> UsageSample:
    if after_step is None:
        return UsageSample(running=running)
    attempts = [item for item in attempts if item.channel == "model" and item.step > after_step]
    requests = [RequestUsage(item.token_usage, item.duration_seconds, window) for item in attempts
                if item.phase == "received"
                and item.completion_status in {"completed", "requires_tool"}]
    unknown = any(item.phase in {"failed", "cancelled"} for item in attempts)
    return _fold(requests, unknown=unknown, running=running)


def request_after_compaction(conversation, record, *, live=None) -> bool:
    """只在压缩边界之后已收到新的完整请求用量时，恢复普通请求统计。"""
    if live is not None and live.kind in {"send", "resume", "program"}:
        after = usage_after_step(live.operation_id, live.started_at, record)
        if after is not None:
            if any(item.channel == "model" and item.phase == "received"
                   and item.step > after
                   and item.completion_status in {"completed", "requires_tool"}
                   and item.token_usage is not None and item.token_usage.input_tokens is not None for item in live.attempts):
                return True
    for turn in reversed(conversation.turns):
        after = usage_after_step(turn.run_id, turn.started_at, record)
        if after is None:
            continue
        steps = (step for step in turn.steps if step.step > after)
        if any(step.completion_status in {"completed", "requires_tool"} and step.token_usage is not None
               and step.token_usage.input_tokens is not None for step in steps):
            return True
    return False


def _fold(requests, *, unknown, running) -> UsageSample:
    latest = next((item for item in reversed(requests) if item.usage is not None and item.usage.input_tokens is not None), None)
    complete = bool(requests) and not unknown and all(
        item.usage is not None and item.usage.input_tokens is not None and item.usage.cache_read_tokens is not None
        and 0 <= item.usage.cache_read_tokens <= item.usage.input_tokens for item in requests)
    return UsageSample(latest,
        sum(item.usage.cache_read_tokens for item in requests) if complete else None,
        sum(item.usage.input_tokens for item in requests) if complete else None,
        len(requests), running)


def cache_percentage(read, total) -> str:
    """一位小数；不足全命中时不四舍五入显示成100%，零分母和缺失仍未知。"""
    if read is None or total is None or total <= 0 or not 0 <= read <= total:
        return "—"
    if read == total:
        return "100.0%"
    value = read / total * 100
    return f"{value:.1f}%" if value < 99.95 else ">99.9%"
