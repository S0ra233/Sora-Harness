"""跨线程只投递固定输入意图；日志仍由当前运行的唯一写入者保存。"""
import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock

from .models import ConversationInput


@dataclass(frozen=True)
class InputNotice:
    item: ConversationInput
    accepted: bool
    error: str | None = None


class ConversationControl:
    """准备阶段暂存投递，绑定后在所属asyncio线程内顺序处理。"""

    def __init__(self, conversation_id: str, run_id: str) -> None:
        self.conversation_id, self.run_id = conversation_id, run_id
        self.on_notice: Callable[[InputNotice], None] | None = None
        self._lock = Lock()
        self._pending: deque[ConversationInput] = deque()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._handler: Callable[[ConversationInput], ConversationInput] | None = None
        self._owner: asyncio.Task | None = None
        self._closed = False
        self.failure: Exception | None = None

    def submit(self, item: ConversationInput) -> bool:
        with self._lock:
            if self._closed or item.origin_run_id != self.run_id:
                return False
            self._pending.append(item)
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self.drain)
            return True

    def bind(self, handler: Callable[[ConversationInput], ConversationInput]) -> None:
        """只在持有journal的后台线程绑定；通知不能拿到可写journal。"""
        with self._lock:
            if self._closed:
                raise ValueError("运行的收件入口已经关闭。")
            self._loop = asyncio.get_running_loop()
            self._owner = asyncio.current_task()
            self._handler = handler
        self.drain()

    def drain(self) -> None:
        """小检查点同步追加和fsync，期间不让另一个回调交错写同一日志。"""
        while True:
            with self._lock:
                if not self._pending or self._handler is None:
                    return
                item = self._pending.popleft()
            try:
                if self.failure is not None:
                    raise self.failure
                saved = self._handler(item)
            except Exception as exc:
                message = str(exc) if isinstance(exc, (OSError, ValueError)) else f"输入保存发生内部错误（{type(exc).__name__}）；草稿保留。"
                self._notify(InputNotice(item, False, message))
                if not isinstance(exc, ValueError):
                    self.failure = exc
                    if self._owner is not None:
                        self._owner.cancel()
            else:
                self._notify(InputNotice(saved, True))

    def close(self) -> None:
        """关闭栅栏先截住新投递，再结算已经接管的意图，最后释放线程引用。"""
        with self._lock:
            self._closed = True
        self.drain()
        with self._lock:
            remaining = tuple(self._pending)
            self._pending.clear()
            self._handler = self._loop = self._owner = None
        for item in remaining:
            self._notify(InputNotice(item, False, "原运行已经结束，输入没有接纳；草稿保留。"))

    def seal_if_empty(self) -> bool:
        """最终回答处原子核对入口；已有待投递输入就让当前运行继续。"""
        with self._lock:
            if self._pending:
                return False
            self._closed = True
            self._handler = self._loop = self._owner = None
        return True

    def _notify(self, notice: InputNotice) -> None:
        if self.on_notice is not None:
            self.on_notice(notice)
