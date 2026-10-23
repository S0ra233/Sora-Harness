"""来源设置复用白色圆角卡片；技能文件仍由用户或插件维护。"""
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ...application.skill_settings import SkillOptions
from ..common.buttons import MotionButton
from ..common.surfaces import CardSurface, WhiteSurface, settings_column
from ..common.switch import SwitchToggle
from ..common.auto_save import FormAutoSave
from .prompt_page import PromptText


class SkillPage(WhiteSurface):
    changed = Signal()

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service, self._dirty, self._loading = service, False, True
        layout = settings_column(self)
        heading = QLabel("技能")
        heading.setObjectName("settingsHeading")
        layout.addWidget(heading)
        note = QLabel("从 / 菜单选择后手动发送；模型也可按名称和说明按需加载。加载只提供资料，不执行脚本。")
        note.setObjectName("settingsDescription")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.project = self._switch(layout, "项目技能", "当前项目目录下的 .agents/skills")
        self.user = self._switch(layout, "用户技能", "用户主目录下的 .agents/skills")
        content = QWidget()
        column = QVBoxLayout(content)
        column.addWidget(QLabel("自定义技能目录"))
        self.roots = PromptText()
        self.roots.setFixedHeight(130)
        self.roots.setPlaceholderText("每行一个绝对目录；目录内使用 技能名/SKILL.md")
        self.roots.setStyleSheet("QPlainTextEdit { background: white; border: none; padding: 4px; }")
        self.roots.textChanged.connect(self._mark)
        column.addWidget(self.roots)
        layout.addWidget(CardSurface(content))
        info = QLabel("已启用插件提供的技能也会显示。同名技能保留来源，不互相覆盖。已有对话的技能正文不会随文件更新改变。")
        info.setWordWrap(True)
        info.setObjectName("settingsNote")
        layout.addWidget(info)
        buttons = QHBoxLayout()
        self.reload_button = MotionButton("重新读取", variant="secondary")
        self.save_button = MotionButton("保存", variant="primary")
        self.save_button.setObjectName("primaryButton")
        buttons.addStretch()
        buttons.addWidget(self.reload_button)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setObjectName("settingsNote")
        layout.addWidget(self.status)
        layout.addStretch()
        self.reload_button.clicked.connect(self.reload)
        self.save_button.clicked.connect(self.save)
        self.reload()
        self._auto = FormAutoSave(self, {"project": self.project, "user": self.user, "roots": self.roots},
            self.save, self.service.settings.path.with_name("skill-form-draft.json"),
            dirty=lambda value: setattr(self, "_dirty", value), status=self.status, ready=lambda: not self._loading)
        self._auto.reset()

    def _switch(self, layout, name, description):
        content = QWidget()
        row = QHBoxLayout(content)
        labels = QVBoxLayout()
        labels.addWidget(QLabel(name))
        note = QLabel(description)
        note.setObjectName("settingsNote")
        labels.addWidget(note)
        row.addLayout(labels, 1)
        switch = SwitchToggle(name)
        switch.setAccessibleName(name)
        switch.toggled.connect(self._mark)
        row.addWidget(switch)
        layout.addWidget(CardSurface(content))
        return switch

    def _mark(self, *_):
        if not self._loading:
            if hasattr(self, "_auto"):
                self._auto.changed()

    def reload(self):
        if hasattr(self, "_auto") and not self._auto.flush():
            return
        try:
            options = self.service.settings.load()
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
            self.save_button.setEnabled(False)
            return
        self._loading = True
        self.project.setChecked(options.project_enabled)
        self.user.setChecked(options.user_enabled)
        self.roots.setPlainText("\n".join(options.custom_roots))
        self._loading, self._dirty = False, False
        self.save_button.setEnabled(True)
        self.service.invalidate()
        self.status.setText("来源已读取；技能目录将在下次打开菜单或发送时刷新。")
        self.changed.emit()
        if hasattr(self, "_auto"):
            self._auto.reset()

    def save(self):
        try:
            options = SkillOptions(self.project.isChecked(), self.user.isChecked(),
                                   tuple(value.strip() for value in self.roots.toPlainText().splitlines() if value.strip()))
            self.service.settings.save(options)
            self.service.invalidate()
        except (OSError, ValueError) as exc:
            self.status.setText(f"保存失败，原设置和当前表单保留：{exc}")
            return False
        self._dirty = False
        self.status.setText("已保存。后续新输入使用这些来源；正在运行的目录和旧消息快照保留。")
        self.changed.emit()
        if hasattr(self, "_auto") and not self._auto._paused:
            self._auto.reset(restore=False)
        return True

    def close_block(self):
        return self._auto.close_block()
