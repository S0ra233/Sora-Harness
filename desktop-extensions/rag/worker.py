"""插件自行持有后台线程；宿主只接收任务登记与关闭检查。"""
import asyncio
from threading import Event, Lock
from time import monotonic
from uuid import uuid4

from PySide6.QtCore import QThread, Signal
from .store import Cancelled


class Worker(QThread):
    progress = Signal(str)
    result = Signal(object)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation_id = uuid4().hex
        self._operation = operation
        self._cancel = Event()
        self._lock = Lock()
        self._loop = self._task = None
        self._last_progress = 0.0

    def _progress(self, message):
        now = monotonic()
        if now - self._last_progress >= .1:
            self._last_progress = now
            self.progress.emit(message)

    def request_cancel(self):
        with self._lock:
            if self._cancel.is_set() or not self.isRunning():
                return False
            self._cancel.set()
            if self._loop is not None and self._task is not None:
                self._loop.call_soon_threadsafe(self._task.cancel)
        return True

    async def _execute(self):
        with self._lock:
            self._loop, self._task = asyncio.get_running_loop(), asyncio.current_task()
        try:
            if self._cancel.is_set():
                raise Cancelled()
            return await self._operation(self._cancel, self._progress)
        finally:
            with self._lock:
                self._loop = self._task = None

    def run(self):
        try:
            value = asyncio.run(self._execute())
        except (Cancelled, asyncio.CancelledError):
            self.result.emit({"status": "cancelled", "message": "已停止；已提交的文件和向量批次保留。"})
        except ValueError as exc:
            self.result.emit({"status": "error", "message": str(exc)})
        except OSError as exc:
            self.result.emit({"status": "error", "message": f"资料读写失败（{type(exc).__name__}，errno={exc.errno}）；原文件保留。"})
        except Exception as exc:
            self.result.emit({"status": "error", "message": f"资料库操作失败（{type(exc).__name__}）；请检查插件实现。"})
        else:
            self.result.emit({"status": "ok", "data": value})


class WorkOwner:
    """结果通知早于线程退出；持有到finished才解除宿主登记。"""
    def _init_jobs(self, tasks):
        self._tasks = tasks
        self._worker = None

    def _start(self, operation, receive):
        if self._worker is not None:
            self.status.setText("当前操作尚未结束，请稍候或点击停止。")
            return
        worker = Worker(operation, self)
        try:
            self._tasks.register(worker.operation_id, "rag", None, worker.request_cancel)
        except ValueError as exc:
            self.status.setText(str(exc))
            worker.deleteLater()
            return
        self._worker = worker
        self.stop.setEnabled(True)
        worker.progress.connect(self.status.setText)
        def result(event):
            self._job_result = event
        self._job_result = None
        worker.result.connect(result)
        def finished():
            self._tasks.release(worker.operation_id)
            self._worker = None
            self.stop.setEnabled(False)
            event = self._job_result
            self._job_result = None
            worker.deleteLater()
            if event is None:
                self.status.setText("后台没有返回结果；原资料保留。")
            elif event["status"] == "ok":
                receive(event["data"])
            else:
                self.status.setText(event["message"])
                failed = getattr(self, "_job_failed", None)
                if failed is not None:
                    failed()
        worker.finished.connect(finished)
        self.status.setText("正在处理…")
        worker.start()

    def _stop(self):
        if self._worker is not None and self._worker.request_cancel():
            self.status.setText("正在停止，等待当前连接和文件操作退出…")

    def _busy_block(self):
        if self._worker is not None:
            from desktop.plugin_api.ui import CloseBlock
            return CloseBlock("资料库操作尚未结束", "请先停止或等待导入、检索或索引操作结束。")
        return None
