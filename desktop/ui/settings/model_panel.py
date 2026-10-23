"""保留设置控件入口；页面表达意图，应用服务负责配置与密钥。"""
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ...application.models import ConnectionSaveError, ModelSettingsService
from ...configuration.models import ConnectionDraft
from ..shell import theme
from .connection_editor import ConnectionEditor
from .connections_page import ConnectionsPage
from ..conversations.worker import ConversationWorker
from ..contracts import CloseBlock
from ..common.surfaces import FadeVeil as _PageVeil
from ..common.buttons import MotionButton
from ..common.auto_save import FormAutoSave


class ModelConfigPanel(QWidget):
    catalog_changed = Signal(object)

    def __init__(self, settings_path: Path, parent=None) -> None:
        super().__init__(parent)
        self._service = ModelSettingsService(settings_path)
        self._retry_draft: ConnectionDraft | None = None
        self._editor: ConnectionEditor | None = None
        self._discovery: ConversationWorker | None = None
        self._discovery_result = None
        self._editing_active = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._connections = ConnectionsPage()
        layout.addWidget(self._connections)
        self._animation = QPropertyAnimation(self)
        self._animation.setPropertyName(b"opacity")
        self._animation.setDuration(theme.PAGE_FADE_DURATION_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._veil = _PageVeil(self)
        self._animation.setTargetObject(self._veil)
        self._animation.finished.connect(self._veil.hide)
        try:
            self._editor = ConnectionEditor(self._service.adapters())
            layout.addWidget(self._editor)
            self._editor.hide()
            self._editor.save_requested.connect(self._save)
            self._editor.cancel_requested.connect(self._cancel)
            self._editor.discover_requested.connect(self._discover_models)
        except ValueError as exc:
            self._connections.set_status(f"适配资料不可用：{exc}")
            self._connections.set_editing_enabled(False)
        self._connections.create_requested.connect(self._new)
        self._connections.edit_requested.connect(self._edit)
        self._connections.activate_requested.connect(self._activate)
        if self._editor is not None:
            self._reload()
            editor = self._editor
            self._auto = FormAutoSave(editor, {"name": editor._name, "url": editor._url, "key": editor._key,
                "provider": editor._provider, "style": editor._style, "complete_url": editor._complete_url,
                "default": editor._default}, lambda: self._save(automatic=True), settings_path.parent / "ui-drafts" / "model-new.json",
                secrets=("key",), status=editor._status, extra=editor.form_extra, restore_extra=editor.restore_form_extra,
                ready=lambda: not editor._filling and self._editing_active and self._discovery is None)
            editor._table.itemChanged.connect(self._auto.changed)
            editor._context.textEdited.connect(self._auto.changed)
            editor._image.activated.connect(self._auto._choice)

    def _bind_auto(self):
        identifier = self._editor._original.connection_id if self._editor._original else "new"
        self._auto.path = self._service.path.parent / "ui-drafts" / ("model-" + identifier + ".json")
        self._auto.reset()

    def _reload(self) -> None:
        try:
            catalog = self._service.list_connections()
            self._connections.set_catalog(catalog)
            self._publish_catalog(catalog)
        except (OSError, ValueError) as exc:
            self._connections.set_status(f"读取接口失败：{exc}；原文件保留。")

    def add_configuration_entry(self, label, callback):
        """设置入口由实际插件贡献；本页不导入插件配置类。"""
        button = MotionButton(label, variant="secondary")
        button.clicked.connect(callback)
        self.layout().insertWidget(0, button)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._editor is not None and not self._editor.isVisible():
            self._reload()

    def _show_page(self, page: QWidget) -> None:
        self._animation.stop()
        for widget in (self._connections, self._editor):
            if widget is not None:
                widget.setVisible(widget is page)
        self._veil.setGeometry(self.rect())
        self._veil.opacity = 1.0
        self._veil.show()
        self._veil.raise_()
        self._animation.setStartValue(1.0)
        self._animation.setEndValue(0.0)
        self._animation.start()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._veil.setGeometry(self.rect())

    def _new(self) -> None:
        if self._editor is None:
            return
        if not self._auto.flush():
            return
        self._retry_draft = None
        self._editor.load(None)
        self._editing_active = True
        self._show_page(self._editor)
        self._bind_auto()

    def _edit(self, identifier: str) -> None:
        if self._editor is None:
            return
        if not self._auto.flush():
            return
        try:
            catalog = self._service.list_connections()
            if identifier not in catalog.connections:
                raise ValueError("接口已变化，请重新加载。")
            self._editor.load(ConnectionDraft(catalog.connections[identifier], identifier))
        except (OSError, ValueError) as exc:
            self._connections.set_status(f"无法编辑：{exc}")
            return
        self._retry_draft = None
        self._editing_active = True
        self._show_page(self._editor)
        self._bind_auto()

    def _activate(self, identifier: str) -> None:
        try:
            catalog = self._service.activate_connection(identifier)
        except (OSError, ValueError) as exc:
            self._connections.set_status(f"启用失败：{exc}")
            return
        self._connections.set_catalog(catalog)
        self._connections.set_status("已启用；新建聊天使用此接口的默认模型。")
        self._publish_catalog(catalog)

    def _save(self, *, automatic=False) -> bool:
        try:
            draft = self._editor.read_draft()
            catalog = self._service.save_connection(draft, self._editor.key_update(), retry_draft=self._retry_draft)
        except ConnectionSaveError as exc:
            self._retry_draft = exc.draft
            self._editor.set_retry(exc.draft)
            self._editor.set_status(str(exc))
            return False
        except (OSError, ValueError) as exc:
            self._editor.set_status(f"保存失败：{exc}；输入保留。")
            return False
        self._retry_draft = None
        self._editor.clear_key()
        self._connections.set_catalog(catalog)
        self._connections.set_status("接口已保存并启用。")
        if automatic:
            identifier = catalog.active_connection
            self._editor._original = ConnectionDraft(catalog.connections[identifier], identifier)
            self._editor._definitions = dict(catalog.connections[identifier].models)
            self._editor._status.setText("已自动保存接口。")
        else:
            self._auto.reset(restore=False)
            self._editing_active = False
            self._show_page(self._connections)
        self._publish_catalog(catalog)
        return True

    def _publish_catalog(self, catalog) -> None:
        """源目录已保存才通知；投影失败不发布空目录或假称同步成功。"""
        try:
            view = self._service.project_catalog(catalog)
        except (OSError, ValueError) as exc:
            self._connections.set_status(f"接口已保存，但更新候选失败：{exc}；请重新打开设置。")
            return
        self.catalog_changed.emit(view)

    def _cancel(self) -> None:
        if not self._auto.flush():
            return
        self._editing_active = False
        self._retry_draft = None
        self._editor.clear_key()
        self._reload()
        self._show_page(self._connections)

    def _discover_models(self) -> None:
        if self._discovery is not None:
            return
        try:
            source, key = self._editor.discovery_source(), self._editor.key_update()
        except ValueError as exc:
            self._editor.set_status(str(exc))
            return

        async def fetch(_):
            return await self._service.list_remote_models(source, key)

        worker = ConversationWorker(fetch, self)
        self._discovery = worker
        self._discovery_result = None
        worker.progress.connect(self._on_discovery)
        worker.finished.connect(self._finish_discovery)
        self._editor.set_discovering(True)
        self._editor.set_status("正在读取接口的模型目录…")
        try:
            worker.start()
        except Exception:
            self._discovery = None
            worker.deleteLater()
            self._editor.set_discovering(False)
            self._editor.set_status("模型目录后台任务未能启动，输入保留。")

    def _on_discovery(self, event) -> None:
        if self._discovery is None or event.operation_id != self._discovery.operation_id:
            return
        if event.kind == "completed":
            self._discovery_result = event.data
        elif event.kind == "failed":
            self._editor.set_status(f"获取模型失败：{event.error}；可继续手动添加。")
        elif event.kind == "cancelled":
            self._editor.set_status("获取模型已取消；输入保留。")

    def _finish_discovery(self) -> None:
        worker, self._discovery = self._discovery, None
        worker.deleteLater()
        self._editor.set_discovering(False)
        models, self._discovery_result = self._discovery_result, None
        if models is None:
            return
        self._editor.set_remote_models(models)
        if not models:
            self._editor.set_status("接口没有返回可用的生成模型，可手动添加。")
            return
        self._editor.set_status(f"已获取 {len(models)} 个模型，可在下拉框选择，或选择加入列表；保存后才写入配置。")

    def close_block(self) -> CloseBlock | None:
        if self._discovery is not None:
            self._discovery.request_cancel()
            return CloseBlock("正在关闭模型目录请求", "请等后台退出后再次关闭窗口；接口输入保留。")
        return self._auto.close_block() if self._editor is not None and self._editing_active else None
