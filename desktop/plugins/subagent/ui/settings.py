"""子角色配置与连接表单分开；默认关闭，保存不请求模型。"""
from dataclasses import replace
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QSpinBox, QVBoxLayout, QWidget

from ....application.subagent_profiles import SubagentProfile, SubagentProfiles
from ....ui.common.buttons import MotionButton
from ....ui.common.disclosure import AnimatedDisclosure
from ....ui.common.surfaces import CardSurface
from ....ui.common.switch import SwitchToggle
from ....ui.common.widgets import ClickComboBox
from ....ui.common.fading_stack import FadingStack
from ....ui.settings.model_panel import ModelConfigPanel
from ....ui.contracts import CloseBlock
from ....ui.common.auto_save import FormAutoSave


class SubagentSettings(QWidget):
    def __init__(self, directory):
        super().__init__()
        self.setWindowTitle("子智能体配置")
        self.service = SubagentProfiles(directory)
        self._editing = None
        self._filling = False
        self._dirty = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setObjectName("settingsNote")
        tabs = QHBoxLayout()
        roles = MotionButton("角色配置", variant="secondary")
        connections = MotionButton("API 接口", variant="secondary")
        tabs.addWidget(roles)
        tabs.addWidget(connections)
        tabs.addStretch()
        outer.addLayout(tabs)
        outer.addWidget(self._status)
        self.stack = FadingStack()
        self._roles = QWidget()
        self._cards = QVBoxLayout(self._roles)
        self._cards.setContentsMargins(0, 0, 0, 0)
        self.stack.addWidget(self._roles)
        self.models = ModelConfigPanel(directory / "subagent-connections.json")
        self.stack.addWidget(self.models)
        self._editor = QWidget()
        form_layout = QVBoxLayout(self._editor)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        form.setSpacing(14)
        self.name = QLineEdit()
        self.description = QLineEdit()
        self.model = ClickComboBox()
        self.effort = ClickComboBox()
        self.instructions = QPlainTextEdit()
        self.instructions.setPlaceholderText("子角色规则；任务与资料由主 Agent 明确传入")
        self.instructions.setFixedHeight(140)
        self.enabled = SwitchToggle("启用此子角色")
        for label, widget in (("角色名称", self.name), ("用途说明", self.description), ("接口／模型", self.model),
                              ("强度设置", self.effort), ("角色规则", self.instructions), ("启用", self.enabled)):
            form.addRow(label, widget)
        content = QWidget()
        content.setLayout(form)
        form_layout.addWidget(CardSurface(content))
        advanced = QWidget()
        limits = QFormLayout(advanced)
        self.timeout, self.steps, self.calls = QSpinBox(), QSpinBox(), QSpinBox()
        self.temperature, self.output_tokens = QLineEdit(), QLineEdit()
        self.temperature.setPlaceholderText("留空使用模型默认值")
        self.output_tokens.setPlaceholderText("留空沿协议默认；思考预算须小于输出上限")
        limits.addRow("温度", self.temperature)
        limits.addRow("最大输出 token", self.output_tokens)
        self.timeout.setRange(1, 86400)
        self.steps.setRange(1, 1000)
        self.calls.setRange(1, 10000)
        for label, widget in (("单次运行总时限（秒）", self.timeout), ("模型步骤上限", self.steps), ("工具调用上限", self.calls)):
            limits.addRow(label, widget)
        advanced_button = MotionButton("高级参数", variant="ghost")
        advanced_button.setCheckable(True)
        disclosure = AnimatedDisclosure(advanced)
        advanced_button.toggled.connect(disclosure.set_expanded)
        form_layout.addWidget(advanced_button)
        form_layout.addWidget(disclosure)
        actions = QHBoxLayout()
        actions.addStretch()
        cancel, save = MotionButton("返回", variant="ghost"), MotionButton("保存", variant="primary")
        actions.addWidget(cancel)
        actions.addWidget(save)
        form_layout.addLayout(actions)
        self.stack.addWidget(self._editor)
        outer.addWidget(self.stack)
        roles.clicked.connect(lambda: self._show_roles())
        connections.clicked.connect(self._show_models)
        cancel.clicked.connect(self._cancel)
        save.clicked.connect(self._save)
        self.models.catalog_changed.connect(lambda _: self._reload_models())
        self.model.currentIndexChanged.connect(self._reload_effort)
        for widget in (self.name, self.description, self.temperature, self.output_tokens):
            widget.textChanged.connect(self._mark_dirty)
        self.instructions.textChanged.connect(self._mark_dirty)
        for widget in (self.model, self.effort):
            widget.currentIndexChanged.connect(self._mark_dirty)
        for widget in (self.timeout, self.steps, self.calls):
            widget.valueChanged.connect(self._mark_dirty)
        self.enabled.toggled.connect(self._mark_dirty)
        self._show_roles()
        self._auto = FormAutoSave(self._editor, {"name": self.name, "description": self.description,
            "model": self.model, "effort": self.effort, "instructions": self.instructions, "enabled": self.enabled,
            "timeout": self.timeout, "steps": self.steps, "calls": self.calls, "temperature": self.temperature,
            "output_tokens": self.output_tokens}, lambda: self._save(automatic=True), directory / "ui-drafts" / "subagent-new.json",
            dirty=lambda value: setattr(self, "_dirty", value), status=self._status,
            ready=lambda: not self._filling and self.stack.currentWidget() is self._editor)

    def close_block(self):
        return self.models.close_block() or self._auto.close_block()

    def _mark_dirty(self, *_):
        if not self._filling:
            if hasattr(self, "_auto"):
                self._auto.changed()

    def _cancel(self):
        if not self._auto.flush():
            return
        self._dirty = False
        self._show_roles()

    def _show_roles(self):
        if hasattr(self, "_auto") and not self._auto.flush():
            return
        if self._dirty and not hasattr(self, "_auto"):
            self._status.setText("请先保存或取消当前修改。")
            return
        while self._cards.count():
            item = self._cards.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        try:
            profiles = self.service.list()
        except (OSError, ValueError, TypeError) as exc:
            self._status.setText(f"角色目录读取失败：{exc}；原文件保留。")
            return
        for profile in profiles:
            body = QWidget()
            row = QHBoxLayout(body)
            text = QVBoxLayout()
            title = QLabel(profile.name)
            title.setTextFormat(Qt.TextFormat.PlainText)
            text.addWidget(title)
            note = QLabel(profile.description or "独立上下文 · 沿父任务工具范围")
            note.setObjectName("settingsNote")
            note.setTextFormat(Qt.TextFormat.PlainText)
            text.addWidget(note)
            row.addLayout(text, 1)
            edit = MotionButton("配置", variant="secondary")
            edit.clicked.connect(lambda _checked=False, value=profile: self._edit(value))
            toggle = SwitchToggle("启用子角色")
            toggle.setChecked(profile.enabled)
            toggle.toggled.connect(lambda checked, value=profile, switch=toggle: self._toggle(value, checked, switch))
            row.addWidget(edit)
            row.addWidget(toggle)
            self._cards.addWidget(CardSurface(body))
        add = MotionButton("新增子角色", variant="secondary")
        add.clicked.connect(lambda: self._edit(None))
        self._cards.addWidget(add)
        self._cards.addStretch()
        self.stack.setCurrentWidget(self._roles)

    def _show_models(self):
        if not self._auto.flush():
            return
        self.stack.setCurrentWidget(self.models)

    def _toggle(self, profile, checked, switch):
        try:
            self.service.save(replace(profile, enabled=checked))
        except (OSError, ValueError) as exc:
            switch.blockSignals(True)
            switch.setChecked(profile.enabled)
            switch.blockSignals(False)
            self._status.setText(str(exc))
            return
        self._show_roles()

    def _reload_models(self):
        selected = self.model.currentData()
        self._filling = True
        try:
            catalog = self.service.models.model_catalog()
            self.model.clear()
            for identifier, entry in catalog.models.items():
                self.model.addItem(f"{entry.connection_label or '接口'} · {entry.label}", identifier)
            index = self.model.findData(selected)
            if index >= 0:
                self.model.setCurrentIndex(index)
        except (OSError, ValueError) as exc:
            self._status.setText(str(exc))
        finally:
            self._filling = False
        self._reload_effort()

    def _reload_effort(self, *_):
        previous = self.effort.currentData()
        self.effort.blockSignals(True)
        self.effort.clear()
        self.effort.addItem("关闭强度设置", None)
        try:
            entry = self.service.models.model_catalog().models.get(self.model.currentData())
            profile = entry.config.reasoning if entry else None
            if profile:
                for value in profile.effort_map:
                    self.effort.addItem(value, value)
            index = self.effort.findData(previous)
            self.effort.setCurrentIndex(max(0, index))
        except (OSError, ValueError) as exc:
            self._status.setText(str(exc))
        finally:
            self.effort.blockSignals(False)

    def _edit(self, profile):
        if hasattr(self, "_auto"):
            if not self._auto.flush():
                return
        if self._dirty and not hasattr(self, "_auto"):
            self._status.setText("请先保存或取消当前修改。")
            return
        self._reload_models()
        self._filling = True
        self._editing = profile.profile_id if profile else uuid4().hex
        self.name.setText(profile.name if profile else "")
        self.description.setText(profile.description if profile else "")
        self.instructions.setPlainText(profile.instructions if profile else "")
        self.enabled.setChecked(profile.enabled if profile else False)
        self.timeout.setValue(int(profile.timeout_seconds) if profile else 900)
        self.steps.setValue(profile.max_steps if profile else 8)
        self.calls.setValue(profile.max_tool_calls if profile else 16)
        self.temperature.setText(str(profile.temperature) if profile and profile.temperature is not None else "")
        self.output_tokens.setText(str(profile.max_output_tokens) if profile and profile.max_output_tokens is not None else "")
        if profile:
            self.model.setCurrentIndex(self.model.findData(profile.model_id))
        self._reload_effort()
        self.effort.setCurrentIndex(max(0, self.effort.findData(profile.effort if profile else None)))
        self._filling = self._dirty = False
        self.stack.setCurrentWidget(self._editor)
        if hasattr(self, "_auto"):
            self._auto.path = self.service.models.path.parent / "ui-drafts" / ("subagent-" + (profile.profile_id if profile else "new") + ".json")
            self._auto.reset()

    def _save(self, _checked=False, *, automatic=False):
        try:
            catalog = self.service.models.model_catalog()
            model_id = self.model.currentData()
            entry = catalog.models.get(model_id)
            if entry is None or entry.connection_id is None:
                raise ValueError("先在API接口中保存可用的子模型。")
            self.service.save(SubagentProfile(self._editing, self.name.text().strip(), entry.connection_id, model_id,
                self.description.text(), self.enabled.isChecked(), self.instructions.toPlainText(), self.effort.currentData(),
                self.timeout.value(), self.steps.value(), self.calls.value(),
                float(self.temperature.text()) if self.temperature.text().strip() else None,
                int(self.output_tokens.text()) if self.output_tokens.text().strip() else None))
        except (OSError, ValueError, TypeError) as exc:
            self._status.setText(f"保存失败：{exc}；表单保留。")
            return False
        self._dirty = False
        self._status.setText("子角色已保存；已受理任务保留原快照。")
        if not automatic:
            self._auto.reset(restore=False)
            self._show_roles()
        return True
