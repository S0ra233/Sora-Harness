"""技能菜单只在后台发现元数据；Qt线程管理归属、菜单和错误展示。"""
from concurrent.futures import CancelledError
from dataclasses import dataclass
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ...skills.catalog import SkillEntry
from ..contracts import CloseBlock
from .commands import LocalCommand


@dataclass(frozen=True)
class SkillMenuResult:
    request_id: str
    key: tuple[str | None, int]
    entries: tuple[SkillEntry, ...] = ()
    issues: tuple[str, ...] = ()
    error: str | None = None


class _SkillMenuWorker(QThread):
    def __init__(self, service, request_id, key, parent):
        super().__init__(parent)
        self.service, self.request_id, self.key = service, request_id, key
        self.result = None

    def _check_cancelled(self):
        if self.isInterruptionRequested():
            raise CancelledError()

    def run(self):
        try:
            scope = self.service.scope(self.key[0], refresh=True, check_cancelled=self._check_cancelled)
            self._check_cancelled()
            self.result = SkillMenuResult(self.request_id, self.key,
                tuple(item for item in scope.entries if item.user_invocable), tuple(scope.issues))
        except CancelledError:
            return
        except Exception as exc:
            message = str(exc) if isinstance(exc, (OSError, ValueError)) else f"技能目录读取失败（{type(exc).__name__}）。"
            self.result = SkillMenuResult(self.request_id, self.key, error=message)


class SkillCommandSource(QObject):
    """每个输入台最多一个扫描；新请求合并为最后一个，退出后才释放线程。"""

    ready = Signal(object)

    def __init__(self, service, parent):
        super().__init__(parent)
        self.service = service
        self._worker = None
        self._pending = None
        self._wanted = None

    def key(self, workspace):
        return workspace, self.service.revision

    @staticmethod
    def definitions(result):
        """在GUI接收结果后创建菜单行为，后台只携带技能元数据。"""
        return tuple(LocalCommand("skill:" + item.skill_id, item.name,
            (item.token, "skill:" + item.name, "skill"), item.description,
            "file", lambda: None, lambda: None, insert_text="/" + item.token + " ", group="技能",
            source="个人" if item.source == "用户" else item.source) for item in result.entries)

    def request(self, key):
        request_id = uuid4().hex
        self._wanted = request_id
        self._pending = (request_id, key)
        if self._worker is not None:
            self._worker.requestInterruption()
        else:
            self._start_pending()
        return request_id

    def _start_pending(self):
        if self._worker is not None or self._pending is None:
            return
        request_id, key = self._pending
        self._pending = None
        worker = _SkillMenuWorker(self.service, request_id, key, self)
        self._worker = worker
        worker.finished.connect(self._finished)
        worker.start()

    @Slot()
    def _finished(self):
        worker, self._worker = self._worker, None
        result = worker.result
        worker.deleteLater()
        if result is not None and result.request_id == self._wanted and result.key[1] == self.service.revision:
            self.ready.emit(result)
        self._start_pending()

    def cancel(self, request_id):
        if request_id != self._wanted:
            return
        self._wanted = None
        self._pending = None
        if self._worker is not None:
            self._worker.requestInterruption()

    def close_block(self):
        if self._worker is not None:
            self.cancel(self._wanted)
            return CloseBlock("正在结束技能目录读取", "已请求结束后台读取，请稍后再次关闭；不会强制中断文件操作。")
        return None
