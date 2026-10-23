"""运行上限、步骤事实与通知；每步仅保存新增输出，不复制整段历史。"""
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ...llm.messages import ModelMessage, ToolCall, ToolResult
from ...llm.models import ModelRequest
from ...llm.contracts import ModelBackend
from ...llm.attempts import ModelAttemptEvent
from ...llm.usage import TokenUsage, summarize_usage
from ...llm.reasoning_output import ReasoningBlock, ReasoningDelta


@dataclass(frozen=True)
class AgentLimits:
    """max_steps 统计主循环模型步骤；联网重发和摘要另计。"""

    max_steps: int = 8
    max_tool_calls: int = 16

    def __post_init__(self) -> None:
        for value in (self.max_steps, self.max_tool_calls):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("Agent 模型步骤与工具调用上限必须是正整数。")


@dataclass
class ToolExecution:
    """调用参数保存在 assistant 消息中，这里记录是否执行及实际结果。"""

    call_id: str
    status: str = "pending"
    # 表示已进入处理函数的执行边界，不代表文件已成功读取。
    handler_started: bool = False
    result: ToolResult | None = None
    # 调度总时长含等待许可；处理时长从真正进入handler开始。旧记录保持未知。
    elapsed_seconds: float | None = None
    handler_seconds: float | None = None


@dataclass
class AgentStep:
    step: int
    completion_status: str = "requesting"
    assistant: ModelMessage | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    tools: list[ToolExecution] = field(default_factory=list)
    # 未完整结束的文本不加入 assistant 执行历史。
    partial_text: str = ""
    token_usage: TokenUsage | None = None
    attempts: list[ModelAttemptEvent] = field(default_factory=list)
    # 实际发出请求前新增的程序资料，恢复时按原位置重放，不在正文界面显示。
    context_messages: list[ModelMessage] = field(default_factory=list)
    reasoning: list[ReasoningBlock] = field(default_factory=list)
    reasoning_active: bool | None = None
    input_ids: list[str] = field(default_factory=list)
    program_event_ids: list[str] = field(default_factory=list)


@dataclass
class AgentResult:
    """单轮事实；跨轮消息和恢复由会话模块管理，不重启本轮旧工具。"""

    initial_request: ModelRequest
    steps: list[AgentStep] = field(default_factory=list)
    stop_reason: str = "running"
    error: str | None = None
    diagnostics: str | None = None
    compactions: list[dict[str, Any]] = field(default_factory=list)
    request_tokens_estimate: list[int] = field(default_factory=list)

    @property
    def text(self) -> str:
        if not self.steps:
            return ""
        last = self.steps[-1]
        return last.assistant.text if last.assistant is not None else last.partial_text

    @property
    def tool_attempts(self) -> int:
        """配额拦截或取消前尚未调度的调用，不计入实际尝试次数。"""
        return sum(
            execution.status not in ("pending", "not_executed")
            for step in self.steps for execution in step.tools
        )

    @property
    def usage_summary(self) -> dict[str, int | None]:
        """统计每次请求结束时的用量，不累计流式片段中的重复计数。"""
        return summarize_usage(
            (step.token_usage for step in self.steps),
            unreported_attempts=sum(event.phase in ("failed", "cancelled") for step in self.steps for event in step.attempts),
        )

    @property
    def http_attempts(self) -> int:
        return sum(event.phase == "started" for step in self.steps for event in step.attempts)

    @property
    def compaction_usage_summary(self) -> dict[str, int | None]:
        return summarize_usage(
            self._compaction_usages(),
            unreported_attempts=sum(event["phase"] in ("failed", "cancelled")
                                    for item in self.compactions for event in item.get("attempts", [])),
        )

    def _compaction_usages(self):
        """新版逐次累计截断与重试；旧记录仍只有最后一次摘要用量。"""
        for item in self.compactions:
            for request in item.get("summary_requests") or (item,):
                raw = request.get("token_usage")
                yield TokenUsage(**raw) if raw is not None else None

    @property
    def total_usage_summary(self) -> dict[str, int | None]:
        """主循环与摘要分别可查；未拿到摘要用量时全程相应字段仍为未知。"""
        return summarize_usage([
            *(step.token_usage for step in self.steps),
            *self._compaction_usages(),
        ], unreported_attempts=(
            self.usage_summary["unreported_attempts"] + self.compaction_usage_summary["unreported_attempts"]
        ))


@dataclass(frozen=True)
class CompactionStatus:
    """压缩过程和有界用量事实，不携带摘要正文或整份历史。"""

    compaction_id: str
    trigger: str
    status: str
    after_step: int = 0
    error: str | None = None
    after_tokens_estimate: int | None = None
    token_usage: TokenUsage | None = None
    summary_requests: tuple[dict, ...] = ()
    usage_unknown: bool = False
    finished_at: str | None = None
    owner_run_id: str | None = None


@dataclass(frozen=True)
class AgentEvent:
    """实时通知用于观察；持久事实来自 AgentResult，而非片段通知的拼接。"""

    kind: str
    step: int
    call: ToolCall | None = None
    result: ToolResult | None = None
    text: str | None = None
    attempt: ModelAttemptEvent | None = None
    compaction: CompactionStatus | None = None
    token_usage: TokenUsage | None = None
    reasoning: ReasoningDelta | None = None
    reasoning_blocks: tuple[ReasoningBlock, ...] = ()
    reasoning_active: bool | None = None
    input_ids: tuple[str, ...] = ()


AgentEventSink = Callable[[AgentEvent], None]


@dataclass(frozen=True)
class AgentCheckpoint:
    """持久检查点与展示通知分开；保存失败必须阻止下一项操作。"""

    kind: str
    step: int
    data: dict[str, Any] = field(default_factory=dict)


AgentCheckpointSink = Callable[[AgentCheckpoint], None]

# 循环只知道请求转换约定，不读取会话日志或压缩配置。
AgentRequestPolicy = Callable[[ModelRequest, ModelBackend], Awaitable[ModelRequest]]


@dataclass(frozen=True)
class AgentInputBatch:
    """边界处选用的用户输入；循环只接消息和编号，不接会话日志。"""

    input_ids: tuple[str, ...] = ()
    messages: tuple[ModelMessage, ...] = ()
    program_event_ids: tuple[str, ...] = ()


AgentInputSource = Callable[[bool], AgentInputBatch]


class AgentCancelled(asyncio.CancelledError):
    """仍是异步取消异常，附带已完成事实，供入口保存后继续传递取消。"""

    def __init__(self, result: AgentResult) -> None:
        super().__init__("Agent 运行已取消")
        self.result = result
