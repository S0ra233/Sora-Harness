"""在后台线程执行插件，将运行通知交回界面线程。"""
import asyncio
from threading import Lock, Event
from process_support import EngineCancelled
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Signal

from ..runtime.application import Runtime
from ..llm.models import ModelRequest
from ..llm.attempts import ModelAttemptEvent
from ..llm.providers.http import ProviderError
from ..runtime.registry import RegistryError
from ..runtime.runs import RunEvent, RunState, exception_diagnostics


class ActionWorker(QThread):
    """每个对象执行一次动作；不在后台读取或修改界面控件。"""

    progress = Signal(object)

    def __init__(
        self, runtime: Runtime, plugin_id: str, action: str,
        payload: dict[str, Any], parent: QObject | None = None,
        *, stream: bool = False,
    ) -> None:
        super().__init__(parent)
        self.run_id = uuid4().hex
        self._runtime = runtime
        self._plugin_id = plugin_id
        self._action = action
        self._payload = dict(payload)
        self._stream = stream
        self._control_lock = Lock()
        self._cancel_requested = False
        self._cancel_event = Event()
        self._terminal_state: RunState | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None

    def run(self) -> None:
        """start() 后由新线程调用；异步网络等待也在这个线程中管理。"""
        self._emit_event(RunState.RUNNING)
        try:
            result = asyncio.run(self._run_action())
        except (asyncio.CancelledError, EngineCancelled):
            self._finish_event(RunState.CANCELLED)
        except (ProviderError, RegistryError, OSError, ValueError) as exc:
            self._finish_event(RunState.FAILED, error=str(exc))
        except Exception as exc:
            # 后台边界仍需结束失败状态，意外错误不回显可能包含密钥的原始消息。
            self._finish_event(
                RunState.FAILED,
                error=f"插件执行发生未处理错误（{type(exc).__name__}），请检查实现。",
                diagnostics=exception_diagnostics(exc),
            )
        else:
            self._finish_event(RunState.SUCCEEDED, result=result)

    async def _run_action(self) -> Any:
        """登记当前异步任务；请求结束前先解除登记，避免取消已关闭的循环。"""
        with self._control_lock:
            self._loop = asyncio.get_running_loop()
            self._task = asyncio.current_task()
            cancelled = self._cancel_requested
        try:
            if cancelled:
                raise asyncio.CancelledError
            return await self._runtime.run_action(
                self._plugin_id, self._action, self._payload,
                on_text=self._on_text if self._stream else None,
                on_request=self._on_request,
                on_attempt=self._on_attempt,
                cancel_event=self._cancel_event,
            )
        finally:
            with self._control_lock:
                self._loop = None
                self._task = None

    def request_cancel(self) -> bool:
        """由界面线程调用，只发取消请求，不等待后台线程结束。"""
        with self._control_lock:
            if self._terminal_state is not None or self._cancel_requested:
                return False
            self._cancel_requested = True
            self._cancel_event.set()
            if self._loop is not None and self._task is not None:
                self._loop.call_soon_threadsafe(self._task.cancel)
        return True

    def _on_text(self, text: str) -> None:
        """跳过已登记取消的片段；界面另外过滤排队中的片段。"""
        with self._control_lock:
            if self._cancel_requested:
                return
        self._emit_event(RunState.RUNNING, text_delta=text)

    def _on_request(self, request: ModelRequest) -> None:
        """保留已交给后端的请求通知，包括取消时仍在界面队列中的通知。"""
        self._emit_event(RunState.RUNNING, request=request)

    def _on_attempt(self, event: ModelAttemptEvent) -> None:
        """只转发联网尝试事实；具体回复布局由插件页面决定。"""
        self._emit_event(RunState.RUNNING, attempt=event)

    def _finish_event(
        self, state: RunState, result: Any = None, error: str | None = None,
        diagnostics: str | None = None,
    ) -> None:
        """实际执行结果决定终态；迟到的取消请求不能抹掉完成或失败事实。"""
        with self._control_lock:
            self._terminal_state = state
        self._emit_event(state, result=result, error=error, diagnostics=diagnostics)

    def _emit_event(
        self, state: RunState, result: Any = None, error: str | None = None,
        text_delta: str | None = None,
        request: ModelRequest | None = None,
        diagnostics: str | None = None,
        attempt: ModelAttemptEvent | None = None,
    ) -> None:
        self.progress.emit(RunEvent(
            run_id=self.run_id, plugin_id=self._plugin_id, action=self._action,
            state=state, result=result, error=error, text_delta=text_delta,
            request=request, diagnostics=diagnostics,
            attempt=attempt,
        ))
