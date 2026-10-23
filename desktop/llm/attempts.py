"""联网尝试的通知；不作为模型消息，也不改变工具执行状态。"""
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from .reasoning_output import ReasoningBlock


@dataclass(frozen=True)
class ModelAttemptEvent:
    """同一次模型请求共享 request_id；重发只增加 attempt。"""

    request_id: str
    attempt: int
    phase: str
    partial_text: str = ""
    error: str | None = None
    retrying: bool = False
    duration_seconds: float | None = None
    # 中断尝试只有公开片段；签名与加密内容不进入这个展示字段。
    reasoning_blocks: tuple[ReasoningBlock, ...] = ()
    reasoning_active: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reasoning_blocks, (tuple, list)):
            raise ValueError("联网尝试的公开思考应为块数组。")
        blocks = tuple(ReasoningBlock(**value) if isinstance(value, dict) else value for value in self.reasoning_blocks)
        if any(not isinstance(value, ReasoningBlock) for value in blocks) or self.reasoning_active is not None and type(self.reasoning_active) is not bool:
            raise ValueError("联网尝试的公开思考或状态无效。")
        object.__setattr__(self, "reasoning_blocks", blocks)
        if not isinstance(self.request_id, str) or not self.request_id or type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("联网尝试需要请求编号和正整数次数。")
        if self.phase not in ("started", "received", "failed", "cancelled"):
            raise ValueError("联网尝试状态不支持。")
        if not isinstance(self.partial_text, str) or type(self.retrying) is not bool or (
            self.error is not None and not isinstance(self.error, str)
        ):
            raise ValueError("联网尝试片段、错误或重试状态无效。")
        if self.duration_seconds is not None and (
            self.phase != "received" or isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, (int, float))
            or not isfinite(self.duration_seconds) or self.duration_seconds < 0
        ):
            raise ValueError("成功请求耗时须为有限的非负秒数。")


ModelAttemptSink = Callable[[ModelAttemptEvent], None]
