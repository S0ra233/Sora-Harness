"""目录的 Qt 桥：持有保存状态和后台摘要扫描，不依赖具体页面。"""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from ...application.catalog import CatalogService, CatalogSnapshot
from ...runtime.conversations import Conversation, ConversationSummary
from ..contracts import CloseBlock
from ..conversations import ConversationHub
from ..conversations.worker import ConversationEvent, ConversationWorker


@dataclass(frozen=True)
class CatalogState:
    ready: bool
    busy: bool
    pending_save: bool
    error: str | None


class CatalogController(QObject):
    """元数据仅在主线程修改；Worker 返回摘要，退出前保留任务登记。"""

    catalog_changed = Signal(object)
    state_changed = Signal(object)
    archiving_changed = Signal(str, bool)

    def __init__(self, path: Path, hub: ConversationHub, parent=None) -> None:
        super().__init__(parent)
        self._path, self._hub = path, hub
        self.service: CatalogService | None = None
        self._worker: ConversationWorker | None = None
        self._error: str | None = None
        self._activity_runs: set[str] = set()
        self._pending_archives: set[str] = set()
        self._finishing_archives: set[str] = set()
        hub.conversation_changed.connect(self._on_conversation)
        hub.event_received.connect(self._on_run_event)
        hub.state_changed.connect(self._on_task_state)

    @property
    def state(self) -> CatalogState:
        return CatalogState(self.service is not None, self._worker is not None,
                            self.service.pending_save if self.service else False, self._error)

    def snapshot(self) -> CatalogSnapshot:
        return self.service.snapshot() if self.service else CatalogSnapshot()

    @Slot()
    def refresh(self) -> None:
        """失败先补存原目录，再只扫描发生变化的日志；不创建／执行会话。"""
        if self._worker is not None:
            return
        try:
            if self.service is None:
                self.service = CatalogService(self._path)
                self.catalog_changed.emit(self.service.snapshot())
            self.service.flush()
        except (OSError, ValueError) as exc:
            self._error = str(exc)
            self.state_changed.emit(self.state)
            return
        application, known = self._hub.application, self.service.journal_signatures()

        async def scan(_notify):
            return await application.summarize_conversations(known)

        worker = ConversationWorker(scan, self)
        worker.progress.connect(self._on_scan)
        worker.finished.connect(self._finish_scan)
        try:
            self._hub.tasks.register(worker.operation_id, "catalog", None, worker.request_cancel)
        except Exception as exc:
            worker.deleteLater()
            self._error = f"目录任务 {worker.operation_id} 未能登记（{type(exc).__name__}），请重试。"
            self.state_changed.emit(self.state)
            return
        self._worker = worker
        self._error = None
        self.state_changed.emit(self.state)
        try:
            worker.start()
        except Exception as exc:
            self._hub.tasks.release(worker.operation_id)
            self._worker = None
            worker.deleteLater()
            self._error = f"目录任务 {worker.operation_id} 的线程未能启动（{type(exc).__name__}），请重试。"
            self.state_changed.emit(self.state)

    def change(self, operation: Callable[[], object]) -> None:
        """改名／置顶／新增项目共用保存反馈；失败保留内存结果。"""
        try:
            operation()
            self._error = None
        except (OSError, ValueError) as exc:
            self._error = str(exc)
        self.catalog_changed.emit(self.snapshot())
        self.state_changed.emit(self.state)

    def is_archiving(self, conversation_id: str) -> bool:
        return conversation_id in self._pending_archives

    def _entry(self, conversation_id: str):
        if self.service is None or self._worker is not None or self.service.pending_save:
            raise ValueError("请先完成会话目录读取或重试保存目录。")
        try:
            return self.service.conversation(conversation_id)
        except KeyError as exc:
            raise ValueError("会话目录没有该聊天，请刷新目录。") from exc

    def archive_requires_stop(self, conversation_id: str) -> bool:
        entry = self._entry(conversation_id)
        if entry.deleted:
            raise ValueError("该聊天已从界面移除。")
        return any(task.conversation_id == conversation_id for task in self._hub.tasks.active())

    def archive(self, conversation_id: str, *, stop_confirmed: bool = False) -> None:
        """确认由页面完成；按全部任务ID停止，等实际释放后归档，不轮询。"""
        try:
            needs_stop = self.archive_requires_stop(conversation_id)
            if conversation_id in self._pending_archives:
                return
            if not needs_stop:
                self._ensure_saved_and_idle(conversation_id)
                self.change(lambda: self.service.archive(conversation_id))
                return
            if not stop_confirmed:
                raise ValueError("此聊天仍有任务，需确认停止并归档。")
            self._pending_archives.add(conversation_id)
            self.archiving_changed.emit(conversation_id, True)
            for task in self._hub.tasks.active():
                if task.conversation_id != conversation_id or task.phase in {"cancelling", "cancelled", "completed", "failed", "stopped", "save_failed"}:
                    continue
                if not self._hub.cancel(task.operation_id):
                    raise ValueError(f"任务 {task.operation_id} 未能停止，归档未完成。")
            self._advance_archive(conversation_id)
        except (OSError, ValueError) as exc:
            self._archive_error(conversation_id, str(exc))

    def _ensure_saved_and_idle(self, conversation_id: str) -> None:
        if any(task.conversation_id == conversation_id for task in self._hub.tasks.active()):
            raise ValueError("该聊天的任务尚未退出，请等待。")
        try:
            controller = self._hub.get(conversation_id)
        except ValueError:
            return
        block = controller.close_block()
        if block is not None:
            raise ValueError(f"聊天 {conversation_id}：{block.message}")

    def _advance_archive(self, conversation_id: str) -> None:
        if conversation_id not in self._pending_archives or conversation_id in self._finishing_archives:
            return
        if any(task.conversation_id == conversation_id for task in self._hub.tasks.active()):
            return
        self._finishing_archives.add(conversation_id)
        try:
            self._ensure_saved_and_idle(conversation_id)
            self._entry(conversation_id)
            self.service.archive(conversation_id)
            self._error = None
        except (OSError, ValueError) as exc:
            self._error = f"归档未完成：{exc}"
        finally:
            self._finishing_archives.discard(conversation_id)
            self._pending_archives.discard(conversation_id)
            self.catalog_changed.emit(self.snapshot())
            self.state_changed.emit(self.state)
            self.archiving_changed.emit(conversation_id, False)

    def _archive_error(self, conversation_id: str, error: str) -> None:
        self._pending_archives.discard(conversation_id)
        self._error = f"归档未完成：{error}"
        self.state_changed.emit(self.state)
        self.archiving_changed.emit(conversation_id, False)

    @Slot(object)
    def _on_task_state(self, state) -> None:
        if state.conversation_id in self._pending_archives:
            self._advance_archive(state.conversation_id)

    def restore(self, conversation_id: str) -> None:
        def restore_entry():
            self._entry(conversation_id)
            self.service.restore(conversation_id)
        self.change(restore_entry)

    def delete_archived(self, conversation_id: str) -> None:
        def delete_entry():
            entry = self._entry(conversation_id)
            if not entry.archived:
                raise ValueError("只有已归档聊天才能删除。")
            if self.is_archiving(conversation_id):
                raise ValueError("归档仍未完成，请等待。")
            self._ensure_saved_and_idle(conversation_id)
            self.service.delete_archived(conversation_id)
        self.change(delete_entry)

    @Slot(object)
    def _on_conversation(self, conversation: Conversation) -> None:
        if self.service is not None:
            self.change(lambda: self.service.observe((ConversationSummary.from_conversation(conversation),)))

    @Slot(object)
    def _on_run_event(self, event: ConversationEvent) -> None:
        if event.kind in {"completed", "cancelled", "failed"}:
            self._activity_runs.discard(event.operation_id)
            return
        if (event.kind != "agent" or event.conversation_id is None or self.service is None
                or event.data.kind != "model_started" or event.data.step != 1 or event.operation_id in self._activity_runs):
            return
        controller = self._hub.get(event.conversation_id)
        view = controller.snapshot().live
        if view is None or view.kind != "send":
            return
        self._activity_runs.add(event.operation_id)
        def note_activity():
            # 通知在开始检查点之后发出；取日志的实际更新时间，不按打开／点击刷新排序。
            stamp = self._hub.application.store.path(event.conversation_id).stat()
            timestamp = datetime.fromtimestamp(stamp.st_mtime_ns / 1_000_000_000, timezone.utc).isoformat()
            self.service.record_activity(event.conversation_id, view.user_text, timestamp)
        self.change(note_activity)

    @Slot(object)
    def _on_scan(self, event: ConversationEvent) -> None:
        if event.kind not in {"completed", "failed", "cancelled"}:
            return
        if event.kind == "completed":
            try:
                self.service.observe(event.data.summaries)
                self._error = "\n".join(event.data.issues) or None
            except (OSError, ValueError) as exc:
                self._error = str(exc)
            self.catalog_changed.emit(self.snapshot())
        elif event.kind == "failed":
            self._error = event.error
        else:
            self._error = "会话目录读取已取消；日志和已登记项目保留。"
        self._hub.tasks.update_phase(event.operation_id, event.kind)
        self.state_changed.emit(self.state)

    @Slot()
    def _finish_scan(self) -> None:
        worker, self._worker = self._worker, None
        self._hub.tasks.release(worker.operation_id)
        worker.deleteLater()
        self.state_changed.emit(self.state)

    def close_block(self) -> CloseBlock | None:
        if self.service is not None and self.service.pending_save:
            return CloseBlock("会话目录待保存", "会话目录的改名、置顶、归档或删除状态尚未保存，请先重试保存目录。会话日志仍保留。")
        return None
