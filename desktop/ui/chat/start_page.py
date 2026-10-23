"""首次发送前的居中输入页；选择与草稿独立于已有后台聊天。"""
from pathlib import Path
from dataclasses import replace
from uuid import NAMESPACE_URL, uuid5

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ...application.conversations import ConversationApplication
from ...application.drafts import InputDraft
from ...attachments.images import MAX_REQUEST_IMAGES
from ...llm.model_catalog import ModelCatalog
from ...settings import load_model_catalog
from ..common.widgets import ElidedLabel, IconGlyph
from ..common.buttons import MotionButton
from ..contracts import CloseBlock
from ..conversations.worker import ConversationWorker
from ..settings.model_picker import ModelPicker
from ..shell import theme
from .composer import Composer
from .project_picker import ProjectPicker, ScopeTray
from .prelude import PreludeEditor


class NewChatPage(QWidget):
    submitted = Signal(object, str, object)
    chat_requested = Signal()
    work_requested = Signal()
    project_requested = Signal(object)

    def __init__(self, settings_path: Path, application: ConversationApplication, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("chatPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._path, self._application = settings_path, application
        self.project_id: str | None = None
        self._choosing_work = False
        self._drafts: dict[str, InputDraft] = {}
        self._dirty: set[str] = set()
        self._draft_errors: dict[str, str] = {}
        self._failed_load: set[str] = set()
        self._model_error = ""
        self._prompt_error = ""
        self._prompt = None
        self.prompt_preset_id = self.prompt_preset_name = None
        self._model_choices: dict[str, tuple[str | None, str | None]] = {}
        self._picker: ModelPicker | None = None
        self._catalog: ModelCatalog | None = None
        self._import: ConversationWorker | None = None
        self._import_target: str | None = None
        self._creating = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(400)
        self._timer.timeout.connect(self.flush_drafts)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 20, 0, 40)
        mode_row = QHBoxLayout()
        mode_row.addStretch()
        segment = QFrame()
        segment.setObjectName("welcomeModes")
        segment.setFixedWidth(240)
        modes = QHBoxLayout(segment)
        modes.setContentsMargins(3, 3, 3, 3)
        modes.setSpacing(0)
        self._mode_group = QButtonGroup(self)
        self._chat_mode = MotionButton("聊天", variant="segment", shape="pill")
        self._work_mode = MotionButton("工作", variant="segment", shape="pill")
        for button in (self._chat_mode, self._work_mode):
            button.setObjectName("welcomeMode")
            button.setCheckable(True)
            button.setFixedHeight(38)
            self._mode_group.addButton(button)
            modes.addWidget(button, 1)
        self._chat_mode.setChecked(True)
        self._chat_mode.clicked.connect(self._request_chat)
        self._work_mode.clicked.connect(self._request_work)
        mode_row.addWidget(segment)
        mode_row.addStretch()
        layout.addLayout(mode_row)
        layout.addStretch(1)
        title = QLabel("我们要做什么？")
        title.setObjectName("welcomeTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addSpacing(20)
        row = QHBoxLayout()
        row.addStretch(1)
        column = QWidget()
        self._column = column
        column.setMaximumWidth(theme.CONTENT_WIDTH)
        contents = QVBoxLayout(column)
        contents.setContentsMargins(0, 0, 0, 0)
        contents.setSpacing(0)
        self.composer = Composer(application.attachments)
        self.prelude = PreludeEditor()
        self.prelude.changed.connect(self._prelude_changed)
        contents.addWidget(self.prelude)
        contents.addWidget(self.composer)
        self._scope = ScopeTray()
        self._scope.setObjectName("welcomeScope")
        self._scope.setFixedHeight(36)
        scope = QHBoxLayout(self._scope)
        scope.setContentsMargins(14, 3, 14, 3)
        scope.addWidget(IconGlyph("folder", size=16))
        # 名称／目录继续保留为页面恢复资料，视觉入口由相邻的项目选择控件承担。
        self._scope_name = ElidedLabel(parent=self._scope)
        self._scope_name.hide()
        self._project_choice = ProjectPicker()
        self._project_choice.setMaximumWidth(300)
        self._project_choice.activated.connect(self._choose_project)
        self._project_choice.popup_closed.connect(self._restore_mode)
        scope.addWidget(self._project_choice)
        scope.addStretch(1)
        scope_row = QHBoxLayout()
        scope_row.setContentsMargins(16, 0, 16, 0)
        scope_row.addWidget(self._scope)
        contents.addLayout(scope_row)
        self._status = QLabel()
        self._status.setObjectName("chatStatus")
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setWordWrap(True)
        contents.addWidget(self._status)
        row.addWidget(column, 100)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        self.composer.draft_changed.connect(self._draft_changed)
        self.composer.send_requested.connect(self._submit)
        self.composer.images_requested.connect(self._attach)
        self.composer.image_removed.connect(self._remove_image)
        self.composer.context.hide()
        self.set_scope(None)

    def set_projects(self, projects) -> None:
        self._project_choice.set_projects(projects)
        self._project_choice.set_project(self.project_id)

    def _request_work(self) -> None:
        # 待选项目也显示工作选中态；实际工具与草稿范围仍只由set_scope修改。
        self._choosing_work = True
        self._work_mode.setChecked(True)
        self.work_requested.emit()
        if not self._project_choice.popup_visible:
            self._restore_mode()

    def _restore_mode(self) -> None:
        if not self._choosing_work:
            return
        self._choosing_work = False
        self._chat_mode.setChecked(self.project_id is None)
        self._work_mode.setChecked(self.project_id is not None)
        self._scope.show()

    def _request_chat(self) -> None:
        self._choosing_work = False
        self._chat_mode.setChecked(self.project_id is None)
        self._work_mode.setChecked(self.project_id is not None)
        self.chat_requested.emit()

    def choose_work_project(self) -> None:
        self._scope.setVisible(True)
        self._column.layout().activate()
        self._scope.layout().activate()
        self._project_choice.showPopup()

    def _choose_project(self, project_id) -> None:
        self.project_requested.emit(project_id)
        self._project_choice.set_project(self.project_id)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._column.setFixedWidth(theme.reading_column_width(self.width()))

    def _draft_id(self) -> str:
        # 这是可恢复的空白页草稿槽，不作为真实聊天或运行编号。
        return uuid5(NAMESPACE_URL, "desktop-start:" + (self.project_id or "ordinary")).hex

    def set_scope(self, project_id: str | None, name: str | None = None, workspace: str | None = None) -> None:
        self._choosing_work = False
        self.project_id = project_id
        self._chat_mode.setChecked(project_id is None)
        self._work_mode.setChecked(project_id is not None)
        self._project_choice.set_project(project_id)
        key = self._draft_id()
        if key not in self._drafts or key in self._failed_load:
            try:
                self._drafts[key] = self._application.drafts.load_input(key)
                self._failed_load.discard(key)
                self._draft_errors.pop(key, None)
            except (OSError, ValueError) as exc:
                self._drafts[key] = InputDraft()
                self._draft_errors[key] = str(exc)
                self._failed_load.add(key)
        self._scope.show()
        self._scope_name.setText(name or workspace or "")
        self._scope_name.setToolTip(workspace or "")
        self._project_choice.setToolTip(workspace or "选择已有项目或创建项目")
        self.reload_models()
        draft = self._drafts[key]
        self.reload_prompt()
        self.composer.restore(draft.text, tuple(draft.images))
        self._status.setText(self._draft_errors.get(key, self._model_error))
        self._update_state()

    @property
    def model_catalog(self) -> ModelCatalog:
        return self._catalog if self._catalog is not None else ModelCatalog()

    def reload_prompt(self) -> None:
        try:
            preset = self._application.prompt_presets.selected_preset()
            self._prompt = preset.config
            self.prompt_preset_id, self.prompt_preset_name = preset.preset_id, preset.name
            draft = self._drafts[self._draft_id()]
            self.prelude.restore(draft.prelude if draft.prelude is not None else self._prompt.prelude)
            self._prompt_error = ""
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._prompt_error = f"提示词预设读取失败：{exc}"
            self._status.setText(self._prompt_error)

    def prompt_config(self):
        if self._prompt_error or self._prompt is None:
            raise ValueError(self._prompt_error or "提示词未加载。")
        return replace(self._prompt, prelude=self.prelude.values())

    def _prelude_changed(self) -> None:
        if self._creating:
            return
        key = self._draft_id()
        self._drafts[key].prelude = self.prelude.values()
        self._dirty.add(key)
        self._timer.start()

    def reload_models(self, catalog: ModelCatalog | None = None) -> None:
        if catalog is None:
            try:
                catalog = load_model_catalog(self._path)
            except (OSError, ValueError) as exc:
                self._model_error = f"读取模型配置失败：{exc}"
                self._status.setText(self._model_error)
                self.composer.set_new_model(None)
                return
        self._model_error = "" if catalog.models else "请先在设置中创建 API 接口。"
        has_tools = self.project_id is not None
        if (self._picker is not None and catalog == self._catalog and has_tools == self._picker_has_tools
                and self._picker_scope == self._draft_id()):
            self._model_changed()
            self._status.setText(self._model_error)
            return
        model_id, effort = self._model_choices.get(self._draft_id(), (catalog.active_model, None))
        same_scope = (self._picker is not None and has_tools == self._picker_has_tools
                      and self._picker_scope == self._draft_id())
        self._catalog, self._picker_has_tools = catalog, has_tools
        self._picker_scope = self._draft_id()
        if same_scope:
            self._picker.set_catalog(catalog)
        else:
            self._picker = ModelPicker(catalog, has_tools, self.composer)
            self.composer.set_model_control(self._picker)
            self._picker.selection_changed.connect(self._remember_model)
        self._picker.restore_selection(model_id, effort)
        self._model_changed()
        self._status.setText(self._model_error)

    def _model_changed(self) -> None:
        entry = self._catalog.selected(self._picker.model_id)
        self.composer.set_new_model(entry.config if entry else None)

    def _remember_model(self) -> None:
        self._model_choices[self._draft_id()] = (self._picker.model_id, self._picker.effort)
        self._model_changed()

    def _draft_changed(self, text: str) -> None:
        key = self._draft_id()
        self._drafts[key].text = text
        self._dirty.add(key)
        self._timer.start()

    def flush_drafts(self) -> bool:
        self._timer.stop()
        for key in tuple(self._dirty):
            draft = self._drafts[key]
            try:
                self._application.drafts.save(key, draft.text, draft.images, prelude=draft.prelude)
            except (OSError, ValueError) as exc:
                self._draft_errors[key] = f"空白页草稿尚未保存：{exc}"
            else:
                self._dirty.remove(key)
                self._draft_errors.pop(key, None)
        if self._draft_id() in self._draft_errors:
            self._status.setText(self._draft_errors[self._draft_id()])
        return not self._draft_errors

    def _submit(self) -> None:
        if self._creating or self._import is not None:
            return
        if self.composer.editor.toPlainText().strip() == "/compact":
            self._status.setText("尚无已结束的对话可压缩；先发送一条聊天消息。")
            return
        try:
            if self._picker is None or self._model_error:
                raise ValueError(self._model_error or "请先保存可用模型。")
            selection = self._picker.selection()
            self.prompt_config()
            self.flush_drafts()
            key = self._draft_id()
            if key in self._draft_errors:
                self._status.setText(self._draft_errors[key])
                return
            draft = self._drafts[key]
            if not draft.text.strip() and not draft.images:
                return
            if draft.images and selection.config.image_input is not True:
                raise ValueError("当前选择的模型未确认支持图片，请更换模型或移除图片。")
            self.submitted.emit(selection, draft.text, tuple(draft.images))
        except (OSError, ValueError) as exc:
            self._status.setText(str(exc))

    def set_creating(self, busy: bool, error: str = "") -> None:
        self._creating = busy
        self._status.setText(error or ("正在准备聊天…" if busy else ""))
        self._update_state()

    def mark_transferred(self) -> None:
        """真实会话已保存草稿后才清除空白页槽；文件仍保留。"""
        key = self._draft_id()
        self._drafts[key] = InputDraft()
        self._model_choices.pop(key, None)
        self._dirty.add(key)
        self.flush_drafts()
        self.composer.restore("", ())
        self.reload_prompt()

    def _update_state(self) -> None:
        self.composer.setEnabled(not self._creating and self._draft_id() not in self._failed_load)
        self.prelude.setEnabled(not self._creating and self._draft_id() not in self._failed_load)
        self.composer.set_state(True, False, self._import is not None)

    def _attach(self, paths) -> None:
        if self._import is not None:
            return
        key = self._draft_id()
        if len(paths) + len(self._drafts[key].images) > MAX_REQUEST_IMAGES:
            self._status.setText("一条消息最多接收 8 张图片。")
            return

        async def receive(_):
            return await self._application.attachments.import_files(paths)

        worker = ConversationWorker(receive, self)
        self._import, self._import_target = worker, key
        worker.progress.connect(self._on_import)
        worker.finished.connect(self._finish_import)
        self._update_state()
        try:
            worker.start()
        except Exception:
            self._import = None
            worker.deleteLater()
            self._status.setText("图片接收未能启动，草稿保留。")
            self._update_state()

    def _on_import(self, event) -> None:
        if self._import is None or event.operation_id != self._import.operation_id:
            return
        if event.kind == "completed":
            key = self._import_target
            draft = self._drafts[key]
            draft.images.extend(info.as_content() for info in event.data)
            self._dirty.add(key)
            self.flush_drafts()
            if key == self._draft_id():
                self.composer.restore(draft.text, tuple(draft.images))
        elif event.kind == "failed":
            self._status.setText(event.error or "图片接收失败。")

    def _finish_import(self) -> None:
        worker, self._import = self._import, None
        self._import_target = None
        worker.deleteLater()
        self._update_state()

    def _remove_image(self, identifier: str) -> None:
        key = self._draft_id()
        draft = self._drafts[key]
        draft.images = [image for image in draft.images if image.attachment_id != identifier]
        self._dirty.add(key)
        self.composer.restore(draft.text, tuple(draft.images))
        self._timer.start()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._creating:
            if self._draft_id() in self._failed_load:
                self.set_scope(self.project_id, self._scope_name.full_text, self._scope_name.toolTip() or None)
            else:
                self.reload_models()

    def hideEvent(self, event) -> None:
        self.flush_drafts()
        super().hideEvent(event)

    def close_block(self) -> CloseBlock | None:
        if self._import is not None:
            self._import.request_cancel()
            return CloseBlock("正在接收图片", "请等待附件后台任务退出后再关闭。")
        if not self.flush_drafts():
            return CloseBlock("空白页草稿尚未保存", "；".join(self._draft_errors.values()))
        return None
