"""将一个应用操作放到后台，沿 Qt 信号回传；不包含会话业务分支。"""
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from threading import Lock
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Signal

from ...runtime.agent.models import AgentEvent
from ...runtime.runs import exception_diagnostics
from ...llm.providers.http import ProviderError
from ...llm.diagnostics import RequestRecordNotice
from ...runtime.conversations.control import ConversationControl


Operation = Callable[[Callable[[AgentEvent | RequestRecordNotice], None]], Awaitable[object]]


@dataclass(frozen=True)
class ConversationEvent:
    operation_id: str
    kind: str
    data: object = None
    error: str | None = None
    diagnostics: str | None = None
    # 控制器固定归属后补充；保持原来的前五个位置参数兼容。
    conversation_id: str | None = None


class ConversationWorker(QThread):
    """每个实例只运行一次；取消请求与事件循环登记使用同一把锁。"""

    progress = Signal(object)

    def __init__(self, operation: Operation, parent: QObject, *, operation_id: str | None = None,
                 control: ConversationControl | None = None) -> None:
        super().__init__(parent)
        self.operation_id = operation_id if operation_id is not None else uuid4().hex
        self._operation = operation
        self.control = control
        if control is not None:
            control.on_notice = lambda notice: self._emit("input", notice)
        self._lock = Lock()
        self._cancel_requested = False
        self._terminal = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        # 只合并界面通知；模型返回、持久检查点和请求记录仍保留原内容。
        self._stream_event: AgentEvent | None = None
        self._stream_fragments: list[str] = []
        self._stream_chars = 0
        self._stream_timer: asyncio.TimerHandle | None = None

    def run(self) -> None:
        self._emit("started")
        try:
            result = asyncio.run(self._execute())
        except asyncio.CancelledError as exc:
            self._finish("cancelled", getattr(exc, "update", getattr(exc, "result", None)))
        except Exception as exc:
            # 原异常仅供控制器识别保存故障；未知异常正文不展示给用户。
            message = str(exc) if isinstance(exc, (OSError, ValueError, ProviderError)) else f"应用操作失败（{type(exc).__name__}）。"
            self._finish("failed", exc, error=message, diagnostics=exception_diagnostics(exc))
        else:
            self._finish("completed", result)

    async def _execute(self) -> object:
        with self._lock:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            cancelled = self._cancel_requested
        try:
            if cancelled:
                raise asyncio.CancelledError
            return await self._operation(lambda event: self._emit(
                "request_record" if isinstance(event, RequestRecordNotice) else "agent", event))
        finally:
            self._flush_stream()
            if self.control is not None:
                self.control.close()
            with self._lock:
                self._loop = None
                self._task = None

    def request_cancel(self) -> bool:
        with self._lock:
            if self._terminal or self._cancel_requested:
                return False
            self._cancel_requested = True
            if self._loop is not None and self._task is not None:
                self._loop.call_soon_threadsafe(self._task.cancel)
        return True

    def _finish(self, kind: str, data: object, *, error: str | None = None, diagnostics: str | None = None) -> None:
        with self._lock:
            self._terminal = True
        if self.control is not None:
            self.control.on_notice = None
        # 最终事实优先；迟到的取消不能抹掉已保存结果或保存故障。
        self._emit(kind, data, error=error, diagnostics=diagnostics)

    def _emit(self, kind: str, data: object = None, *, error: str | None = None, diagnostics: str | None = None) -> None:
        if kind == "agent" and self._buffer_stream(data):
            return
        # 状态通知不能越过之前的文字；错误、取消和工具结果也会先结算文字缓冲。
        self._flush_stream()
        self.progress.emit(ConversationEvent(self.operation_id, kind, data, error, diagnostics))

    def _buffer_stream(self, event: object) -> bool:
        if not isinstance(event, AgentEvent):
            return False
        if event.kind == "text_delta" and event.text:
            identity, text = replace(event, text=None), event.text
        elif event.kind == "reasoning_delta" and event.reasoning is not None and event.reasoning.status == "delta" and event.reasoning.text:
            identity = replace(event, reasoning=replace(event.reasoning, text=""))
            text = event.reasoning.text
        else:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if self._stream_event is not None and self._stream_event != identity:
            self._flush_stream()
        self._stream_event = identity
        self._stream_fragments.append(text)
        self._stream_chars += len(text)
        # 计时从首片段开始，不随新片段延后；持续输出也能定期到达界面。
        if self._stream_chars >= 65536:
            self._flush_stream()
        elif self._stream_timer is None:
            self._stream_timer = loop.call_later(0.025, self._flush_stream)
        return True

    def _flush_stream(self) -> None:
        if self._stream_timer is not None:
            self._stream_timer.cancel()
            self._stream_timer = None
        event, self._stream_event = self._stream_event, None
        if event is None:
            return
        text = "".join(self._stream_fragments)
        self._stream_fragments.clear()
        self._stream_chars = 0
        event = (replace(event, text=text) if event.kind == "text_delta"
                 else replace(event, reasoning=replace(event.reasoning, text=text)))
        self.progress.emit(ConversationEvent(self.operation_id, "agent", event))
