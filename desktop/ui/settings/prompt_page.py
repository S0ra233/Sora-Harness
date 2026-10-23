"""分类提示词表单；字段保持可还原，预设不影响已有会话快照。"""
from dataclasses import asdict
from uuid import uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QVBoxLayout, QWidget

from ...application.prompt_presets import DEFAULT_ID, PromptPreset
from ...prompts import PromptConfig, load_prompt_config
from ..common.buttons import MotionButton
from ..common.dialogs import SurfaceDialog
from ..common.widgets import ClickComboBox
from ..common.disclosure import AnimatedDisclosure
from ..common.surfaces import CardSurface, WhiteSurface, settings_column
from ..common.auto_save import FormAutoSave
from ..shell import theme


class PromptText(QPlainTextEdit):
    """长文本独立滚动，到边界交还设置页；短文本不吞滚轮。"""

    def wheelEvent(self, event) -> None:
        bar = self.verticalScrollBar()
        delta = event.pixelDelta().y() or event.angleDelta().y()
        if bar.maximum() > bar.minimum() and ((delta > 0 and bar.value() > bar.minimum())
                                             or (delta < 0 and bar.value() < bar.maximum())):
            super().wheelEvent(event)
        else:
            event.ignore()


FIELDS = (("system", "output_style", "输出风格"), ("system", "output_format", "输出格式"),
          ("context", "user_preferences", "用户偏好"), ("context", "project_instructions", "项目参考"),
          ("context", "task_context", "任务参考"), ("application", "environment_guidance", "环境规则"),
          ("application", "task_strategy", "工作策略"), ("application", "action_care", "操作注意事项"),
          ("application", "tool_guidance", "工具规则"))


class PromptPage(WhiteSurface):
    changed = Signal()

    def __init__(self, presets, parent=None) -> None:
        super().__init__(parent)
        self._presets, self._loading, self._dirty = presets, True, False
        self._items, self._drafts, self._current, self._base = {}, {}, None, None
        self._pending, self._selected = set(), DEFAULT_ID
        layout = settings_column(self)
        heading = QLabel("提示词")
        heading.setObjectName("settingsHeading")
        layout.addWidget(heading)
        note = QLabel("保存的预设用于新聊天；已有聊天可在会话菜单中主动应用。留空可关闭对应规则。")
        note.setWordWrap(True)
        note.setObjectName("settingsDescription")
        layout.addWidget(note)
        row = QHBoxLayout()
        self._choice = ClickComboBox()
        self._name = QLineEdit()
        self._name.setPlaceholderText("预设名称")
        self._new = MotionButton("新建", variant="secondary")
        self._save = MotionButton("保存", variant="primary")
        self._save.setObjectName("primaryButton")
        self._delete = MotionButton("删除", variant="danger")
        self._delete.setObjectName("dangerButton")
        self._delete.setToolTip("删除当前预设；默认预设恢复内置内容")
        for widget in (self._choice, self._name, self._new, self._save, self._delete):
            row.addWidget(widget)
        layout.addLayout(row)
        self._fields = {}
        self._add_field(layout, "system", "identity", "系统提示词", 180)
        self._add_field(layout, "context", "user_profile", "用户画像", 150)
        self._advanced_button = MotionButton("高级选项", variant="ghost")
        self._advanced_button.setCheckable(True)
        layout.addWidget(self._advanced_button, 0, Qt.AlignmentFlag.AlignLeft)
        advanced = QWidget()
        fields = QVBoxLayout(advanced)
        fields.setContentsMargins(0, 0, 0, 0)
        for category, key, label in FIELDS:
            self._add_field(fields, category, key, label, 90)
        self._advanced = AnimatedDisclosure(advanced)
        self._advanced_button.toggled.connect(self._advanced.set_expanded)
        layout.addWidget(self._advanced)
        self._status = QLabel()
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setWordWrap(True)
        self._status.setObjectName("settingsNote")
        layout.addWidget(self._status)
        layout.addStretch()
        self._choice.currentIndexChanged.connect(self._switch)
        self._name.textChanged.connect(self._mark_dirty)
        self._save.clicked.connect(self._persist)
        self._new.clicked.connect(self._create)
        self._delete.clicked.connect(self._delete_current)
        self.reload()
        fields = {"name": self._name, **{category + "/" + key: widget for (category, key), widget in self._fields.items()}}
        self._auto = FormAutoSave(self, fields, lambda: self._persist(automatic=True), self._draft_path(),
            dirty=lambda value: setattr(self, "_dirty", value), status=self._status, ready=lambda: not self._loading)
        self._auto.reset()

    def _draft_path(self):
        key = "new" if self._current in self._pending else self._current
        return self._presets.path.parent / "ui-drafts" / ("prompt-" + key + ".json")

    def _bind_auto(self):
        if hasattr(self, "_auto"):
            self._auto.path = self._draft_path()
            self._auto.reset()

    def _add_field(self, layout, category, key, title, height) -> None:
        content = QWidget()
        column = QVBoxLayout(content)
        column.setContentsMargins(8, 6, 8, 6)
        label = QLabel(title)
        label.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600;")
        editor = PromptText()
        editor.setFixedHeight(height)
        editor.setPlaceholderText("留空关闭")
        editor.setStyleSheet(f"background: {theme.BACKGROUND}; color: {theme.TEXT}; border: none; padding: 4px;")
        column.addWidget(label)
        column.addWidget(editor)
        layout.addWidget(CardSurface(content))
        self._fields[category, key] = editor
        editor.textChanged.connect(self._mark_dirty)

    def _mark_dirty(self, *_):
        if not self._loading:
            if hasattr(self, "_auto"):
                self._auto.changed()

    def _form(self):
        values = asdict(self._base)
        for (category, key), editor in self._fields.items():
            text = editor.toPlainText()
            if text or key in values[category]:
                values[category][key] = text
        return PromptConfig(**values)

    def _display(self, config, name) -> None:
        self._loading = True
        self._base = config
        self._name.setText(name)
        for (category, key), editor in self._fields.items():
            editor.setPlainText(getattr(config, category).get(key, ""))
        self._loading = False

    def _switch(self, *_):
        if self._loading:
            return
        selected = self._choice.currentData()
        if hasattr(self, "_auto"):
            if not self._auto.flush():
                self._choice.blockSignals(True)
                self._choice.setCurrentIndex(self._choice.findData(self._current))
                self._choice.blockSignals(False)
                return
        self._remember_current()
        self._current = selected
        preset = self._items[self._current]
        self._display(*self._drafts.get(self._current, (preset.config, preset.name)))
        self._update_dirty()
        self._bind_auto()
        if selected not in self._pending:
            try:
                self._presets.select(selected)
                self._selected = selected
                self.changed.emit()
            except (OSError, ValueError) as exc:
                self._status.setText(f"预设选择未保存：{exc}；可点击保存重试。")

    def _remember_current(self) -> None:
        if self._current is not None and self._base is not None:
            self._drafts[self._current] = (self._form(), self._name.text())

    def _update_dirty(self) -> None:
        self._dirty = bool(self._pending) or any(
            key in self._items and (values[0] != self._items[key].config or values[1] != self._items[key].name)
            for key, values in self._drafts.items()
        )

    def _show_presets(self, items, current: str) -> None:
        # 刷新已保存条目时保留其他条目的草稿，新建尚未保存的条目也不丢失。
        pending = {key: value for key, value in self._items.items() if key in self._pending}
        self._items = {preset.preset_id: preset for preset in items}
        self._items.update(pending)
        self._drafts = {key: value for key, value in self._drafts.items() if key in self._items}
        self._loading = True
        self._choice.clear()
        for preset in self._items.values():
            name = self._drafts.get(preset.preset_id, (preset.config, preset.name))[1]
            self._choice.addItem(name.strip() or "未命名预设", preset.preset_id)
        self._choice.setCurrentIndex(self._choice.findData(current))
        self._current = current
        preset = self._items[current]
        self._display(*self._drafts.get(current, (preset.config, preset.name)))
        self._update_dirty()

    def _create(self) -> None:
        if hasattr(self, "_auto"):
            if not self._auto.flush():
                return
        try:
            config = load_prompt_config()
        except (OSError, ValueError) as exc:
            self._status.setText(f"新建失败，当前表单保留：{exc}")
            return
        self._remember_current()
        identifier = uuid4().hex
        self._items[identifier] = PromptPreset(identifier, "", config)
        self._pending.add(identifier)
        self._show_presets(tuple(self._items.values()), identifier)
        self._status.setText("填写名称后保存新预设；当前内容采用内置默认。")
        self._name.setFocus()
        self._bind_auto()

    def reload(self) -> None:
        if hasattr(self, "_auto") and not self._auto.flush():
            return
        try:
            items, selected = self._presets.load()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._status.setText(f"预设读取失败，原文件保留：{exc}")
            self._save.setEnabled(False)
            self._new.setEnabled(False)
            self._delete.setEnabled(False)
            return
        self._remember_current()
        self._selected = selected
        self._show_presets(items, selected)
        self._save.setEnabled(True)
        self._new.setEnabled(True)
        self._delete.setEnabled(True)
        self._status.setText("")
        self._bind_auto()

    def _persist(self, _checked=False, *, automatic=False) -> bool:
        try:
            preset = self._items[self._current]
            config = self._form()
            new = self._current in self._pending
            if not new and config == preset.config and self._name.text().strip() == preset.name:
                self._presets.select(self._current)
                saved = preset
            else:
                saved = self._presets.save(self._name.text(), config, None if new else self._current)
            self._drafts.pop(self._current, None)
            if new:
                self._pending.remove(self._current)
                self._items.pop(self._current)
            self._items[saved.preset_id] = saved
            self._selected = saved.preset_id
            if automatic:
                index = self._choice.findData(self._current)
                self._current, self._base = saved.preset_id, saved.config
                self._choice.blockSignals(True)
                if index >= 0:
                    self._choice.setItemText(index, saved.name)
                    self._choice.setItemData(index, saved.preset_id)
                self._choice.blockSignals(False)
            else:
                self._show_presets(tuple(self._items.values()), saved.preset_id)
                self._auto.reset(restore=False)
            self._status.setText("已保存，新聊天使用此预设。")
            self.changed.emit()
            return True
        except (OSError, ValueError) as exc:
            self._status.setText(f"保存失败，表单和原预设保留：{exc}")
            return False

    def _delete_current(self) -> None:
        if not self._auto.flush():
            return
        identifier = self._current
        restore = identifier == DEFAULT_ID
        name = self._name.text().strip() or self._items[identifier].name or "未命名预设"
        dialog = SurfaceDialog("恢复默认预设？" if restore else "删除预设？", self, width=460)
        description = ("恢复内置默认内容及名称，当前修改将被丢弃。" if restore
                       else f"删除“{name}”及该条目未保存的修改。")
        label = QLabel(description + "已有对话继续保留原提示词。")
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        dialog.body.addWidget(label)
        confirm = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        confirm.setText("恢复" if restore else "删除")
        confirm.setObjectName("dangerButton")
        confirm.set_variant("danger")
        dialog.buttons.accepted.connect(dialog.accept)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        dialog.deleteLater()
        if not accepted:
            return
        try:
            local_only = identifier in self._pending
            if local_only:
                items = tuple(preset for preset in self._items.values() if preset.preset_id != identifier)
                selected = self._selected
            else:
                items, selected = self._presets.delete(identifier)
            self._pending.discard(identifier)
            self._drafts.pop(identifier, None)
            self._items.pop(identifier, None)
            self._selected = selected
            self._auto.reset(restore=False)
            self._show_presets(items, selected)
            self._bind_auto()
            self._status.setText("已恢复内置默认内容。" if restore else "已删除当前预设。")
            if not local_only:
                self.changed.emit()
        except (OSError, ValueError) as exc:
            action = "恢复" if restore else "删除"
            self._status.setText(f"{action}失败，表单和原预设保留：{exc}")

    def close_block(self):
        return self._auto.close_block()
