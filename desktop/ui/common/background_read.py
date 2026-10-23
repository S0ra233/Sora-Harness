"""小型页面的后台操作；线程真正退出后才解除关闭检查。"""
import asyncio
from PySide6.QtCore import QObject, Signal
from ..conversations.worker import ConversationWorker
from ..contracts import CloseBlock


class BackgroundRead(QObject):
    completed = Signal(object)
    failed = Signal(str)
    settled = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.worker = None

    def start(self, function):
        if self.worker is not None:
            return False
        async def operation(_):
            pending = asyncio.create_task(asyncio.to_thread(function))
            try:
                return await asyncio.shield(pending)
            except asyncio.CancelledError:
                await asyncio.gather(pending, return_exceptions=True)
                raise
        worker = ConversationWorker(operation, self)
        self.worker = worker
        def receive(event):
            if event.kind == "completed":
                self.completed.emit(event.data)
            elif event.kind == "failed":
                self.failed.emit(event.error or "操作失败，已有内容保留。")
        def finished():
            self.worker = None
            worker.deleteLater()
            self.settled.emit()
        worker.progress.connect(receive)
        worker.finished.connect(finished)
        try:
            worker.start()
        except Exception as exc:
            self.worker = None
            worker.deleteLater()
            self.failed.emit(f"后台操作无法启动（{type(exc).__name__}）；已有内容保留。")
            self.settled.emit()
            return False
        return True

    def close_block(self):
        return CloseBlock("操作尚未结束", "请等待文件操作结束后再关闭。") if self.worker else None
