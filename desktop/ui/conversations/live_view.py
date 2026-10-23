"""会话显示的临时投影；不用于恢复模型历史，也不执行工具。"""
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic

from ...llm.attempts import ModelAttemptEvent
from ...llm.usage import TokenUsage
from ...llm.messages import ImageContent, ToolCall, ToolResult
from ...llm.reasoning_output import ReasoningBlock
from ...runtime.agent.models import AgentEvent, CompactionStatus
from ...runtime.conversations.models import ConversationInput


@dataclass(frozen=True)
class LiveAttempt:
    channel: str
    step: int
    request_id: str | None
    attempt: int | None
    phase: str
    text: str
    error: str | None = None
    retrying: bool = False
    completion_status: str | None = None
    token_usage: TokenUsage | None = None
    duration_seconds: float | None = None
    reasoning: tuple[ReasoningBlock, ...] = ()
    reasoning_active: bool | None = None
    reasoning_running: bool = False
    reasoning_running_blocks: frozenset[str] = frozenset()


@dataclass(frozen=True)
class LiveTool:
    step: int
    call: ToolCall
    phase: str
    result: ToolResult | None = None


@dataclass(frozen=True)
class LiveSnapshot:
    operation_id: str
    kind: str
    user_text: str
    user_images: tuple[ImageContent, ...]
    user_recorded: bool
    attempts: tuple[LiveAttempt, ...]
    tools: tuple[LiveTool, ...]
    compactions: tuple[CompactionStatus, ...] = ()
    elapsed_seconds: int | None = None
    started_at: str | None = None
    inputs: tuple[ConversationInput, ...] = ()
    step_inputs: tuple[tuple[int, tuple[str, ...]], ...] = ()
    source_input_id: str | None = None


@dataclass
class _AttemptBuffer:
    channel: str
    step: int
    event: ModelAttemptEvent | None = None
    fragments: list[str] = field(default_factory=list)
    completion_status: str | None = None
    token_usage: TokenUsage | None = None
    _text: str = ""
    reasoning: dict[str, ReasoningBlock] = field(default_factory=dict)
    reasoning_pending: dict[str, tuple[str, list[str]]] = field(default_factory=dict)
    reasoning_active: bool | None = None
    reasoning_running: bool = False
    reasoning_running_blocks: set[str] = field(default_factory=set)
    _snapshot: LiveAttempt | None = None

    def snapshot(self) -> LiveAttempt:
        if self._snapshot is not None:
            return self._snapshot
        event = self.event
        if self.fragments:
            self._text += "".join(self.fragments)
            self.fragments.clear()
        for identifier, (kind, parts) in self.reasoning_pending.items():
            previous = self.reasoning.get(identifier, ReasoningBlock(identifier, "", kind))
            self.reasoning[identifier] = ReasoningBlock(identifier, previous.text + "".join(parts), kind)
        self.reasoning_pending.clear()
        text = event.partial_text if event is not None and event.partial_text else self._text
        self._snapshot = LiveAttempt(
            self.channel, self.step, event.request_id if event else None, event.attempt if event else None,
            event.phase if event else "started", text, event.error if event else None,
            event.retrying if event else False, self.completion_status, self.token_usage,
            event.duration_seconds if event else None,
            tuple(self.reasoning.values()), self.reasoning_active, self.reasoning_running,
            frozenset(self.reasoning_running_blocks),
        )
        return self._snapshot


class LiveRunView:
    """片段增量写入，切回时才拼快照；每次 HTTP 重发保留独立展示。"""

    def __init__(self, operation_id: str, kind: str, user_text: str = "",
                 user_images: tuple[ImageContent, ...] = (), *, source_input_id: str | None = None) -> None:
        self.operation_id, self.kind = operation_id, kind
        self.user_text, self.user_images = user_text, user_images
        self.user_recorded = False
        self.started_at = datetime.now(timezone.utc).isoformat()
        self._started, self._finished = monotonic(), None
        self._attempts: dict[tuple[str, int, str, int], _AttemptBuffer] = {}
        self._current: dict[tuple[str, int], _AttemptBuffer] = {}
        self._tools: dict[str, LiveTool] = {}
        self._compactions: dict[str, CompactionStatus] = {}
        self._inputs: dict[str, ConversationInput] = {}
        self._step_inputs: dict[int, tuple[str, ...]] = {}
        self.source_input_id = source_input_id

    def record_input(self, item: ConversationInput) -> None:
        self._inputs[item.input_id] = item

    @property
    def latest_compaction(self) -> CompactionStatus | None:
        """仅使用已落盘成功通知，不把摘要开始或失败当成锁定边界。"""
        return next((item for item in reversed(self._compactions.values())
                     if item.status == "succeeded" and item.finished_at), None)

    def finish(self) -> None:
        if self._finished is None:
            self._finished = monotonic()

    def record(self, event: AgentEvent) -> None:
        if event.kind in {"compaction_started", "compaction_finished"} and event.compaction is not None:
            self._compactions[event.compaction.compaction_id] = event.compaction
        elif event.kind == "model_started":
            self.user_recorded = True
            self._step_inputs[event.step] = event.input_ids
        elif event.kind in {"model_attempt", "compaction_model_attempt"} and event.attempt is not None:
            channel = "summary" if event.kind == "compaction_model_attempt" else "model"
            attempt = event.attempt
            key = (channel, event.step, attempt.request_id, attempt.attempt)
            buffer = self._attempts.setdefault(key, _AttemptBuffer(channel, event.step))
            buffer.event = attempt
            if attempt.phase in {"failed", "cancelled"}:
                buffer.reasoning = {block.block_id: block for block in attempt.reasoning_blocks}
                buffer.reasoning_pending.clear()
                buffer.reasoning_active = attempt.reasoning_active
                buffer.reasoning_running = False
                buffer.reasoning_running_blocks.clear()
            buffer._snapshot = None
            self._current[channel, event.step] = buffer
        elif event.kind == "text_delta" and event.text is not None:
            key = ("model", event.step)
            buffer = self._current.get(key)
            if buffer is None:
                buffer = _AttemptBuffer("model", event.step)
                self._attempts["model", event.step, "", 0] = buffer
                self._current[key] = buffer
            buffer.fragments.append(event.text)
            buffer.reasoning_running = False
            buffer.reasoning_running_blocks.clear()
            buffer._snapshot = None
        elif event.kind == "reasoning_delta" and event.reasoning is not None:
            buffer = self._current.get(("model", event.step))
            if buffer is not None:
                delta = event.reasoning
                buffer.reasoning_active = True
                if delta.status == "finished":
                    buffer.reasoning_running_blocks.discard(delta.block_id)
                else:
                    buffer.reasoning_running_blocks.discard("pending")
                    buffer.reasoning_running_blocks.add(delta.block_id)
                buffer.reasoning_running = bool(buffer.reasoning_running_blocks)
                if delta.text:
                    _, parts = buffer.reasoning_pending.setdefault(delta.block_id, (delta.kind, []))
                    parts.append(delta.text)
                buffer._snapshot = None
        elif event.kind == "model_finished":
            buffer = self._current.get(("model", event.step))
            if buffer is not None:
                buffer.completion_status = event.text
                buffer.token_usage = event.token_usage
                buffer.reasoning = {block.block_id: block for block in event.reasoning_blocks}
                buffer.reasoning_pending.clear()
                buffer.reasoning_active = event.reasoning_active
                buffer.reasoning_running = False
                buffer.reasoning_running_blocks.clear()
                buffer._snapshot = None
        elif event.kind == "tool_started" and event.call is not None:
            self._tools[event.call.call_id] = LiveTool(event.step, deepcopy(event.call), "running")
        elif event.kind == "tool_finished" and event.call is not None and event.result is not None:
            previous = self._tools.get(event.call.call_id)
            phase = "succeeded" if not event.result.is_error else "failed" if previous else "rejected"
            self._tools[event.call.call_id] = LiveTool(event.step, deepcopy(event.call), phase, deepcopy(event.result))

    def snapshot(self) -> LiveSnapshot:
        return LiveSnapshot(
            self.operation_id, self.kind, self.user_text, self.user_images, self.user_recorded,
            tuple(buffer.snapshot() for buffer in self._attempts.values()),
            tuple(self._tools.values()),
            tuple(self._compactions.values()),
            max(0, int((self._finished if self._finished is not None else monotonic()) - self._started)),
            self.started_at,
            tuple(self._inputs.values()), tuple(self._step_inputs.items()), self.source_input_id,
        )
