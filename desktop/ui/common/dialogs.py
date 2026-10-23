"""项目、新会话和改名共用的圆角弹窗外壳；只提供布局与标准确认信号。"""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QVBoxLayout,
)

from .widgets import IconButton
from .surfaces import paint_card
from .buttons import MotionButton
from .focus import FocusReturn
from ..shell import theme


class _DialogButtons(QDialogButtonBox):
    """保留标准按钮查询和确认信号，按钮绘制使用主界面的共用组件。"""

    def __init__(self) -> None:
        super().__init__()
        confirm, cancel = MotionButton("确定", variant="primary", size="medium"), MotionButton("取消", variant="ghost", size="medium")
        confirm.setObjectName("primaryButton")
        confirm.setDefault(True)
        cancel.setObjectName("cancelButton")
        for button, role in ((confirm, QDialogButtonBox.ButtonRole.AcceptRole), (cancel, QDialogButtonBox.ButtonRole.RejectRole)):
            button.setMinimumHeight(36)
            self.addButton(button, role)
        self._standard = {QDialogButtonBox.StandardButton.Ok: confirm, QDialogButtonBox.StandardButton.Cancel: cancel}

    def button(self, which):
        return self._standard.get(which) or super().button(which)


class SurfaceDialog(QDialog):
    def __init__(self, title: str, parent=None, *, width: int = 540) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_WindowPropagation)
        self.setStyleSheet(theme.dialog_stylesheet())
        self.setMinimumWidth(width)
        self._focus_return = FocusReturn(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 22)
        surface = QFrame()
        surface.setObjectName("dialogSurface")
        outer.addWidget(surface)
        layout = QVBoxLayout(surface)
        layout.setContentsMargins(26, 22, 26, 24)
        layout.setSpacing(18)
        heading = QHBoxLayout()
        label = QLabel(title)
        label.setObjectName("dialogTitle")
        label.setTextFormat(Qt.TextFormat.PlainText)
        close = IconButton("close", "关闭")
        close.clicked.connect(self.reject)
        heading.addWidget(label, 1)
        heading.addWidget(close)

        layout.addLayout(heading)
        self.body = QVBoxLayout()
        self.body.setSpacing(14)
        layout.addLayout(self.body)
        self.buttons = _DialogButtons()
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            paint_card(painter, QRectF(self.rect()).adjusted(18, 18, -18, -22),
                       radius=theme.DIALOG_RADIUS, shadow=12, background=theme.BACKGROUND)
        finally:
            painter.end()

    def setVisible(self, visible: bool) -> None:
        if visible and not self.isVisible():
            self._focus_return.capture(self.parentWidget())
        super().setVisible(visible)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._focus_return.restore()


class NameDialog(SurfaceDialog):
    def __init__(self, original: str, parent=None) -> None:
        super().__init__("编辑会话名称", parent, width=440)
        self.input = QLineEdit(original)
        self.input.setPlaceholderText("会话名称")
        self.body.addWidget(self.input)
        confirm = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        confirm.setText("保存")
        self.input.textChanged.connect(lambda text: confirm.setEnabled(bool(text.strip())))
        confirm.setEnabled(bool(original.strip()))
        self.buttons.accepted.connect(self.accept)
        self.input.selectAll()
        self.input.setFocus()


class ArchiveConfirmation(SurfaceDialog):
    def __init__(self, parent=None) -> None:
        super().__init__("停止并归档此聊天？", parent, width=530)
        description = QLabel("归档会停止此聊天所有正在进行的工作。你可以稍后在设置中恢复该聊天。")
        description.setWordWrap(True)
        description.setTextFormat(Qt.TextFormat.PlainText)
        self.body.addWidget(description)
        confirm = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        confirm.setObjectName("dangerButton")
        confirm.set_variant("danger")
        confirm.setText("停止并归档")
        self.buttons.accepted.connect(self.accept)


class HistoryConfirmation(SurfaceDialog):
    def __init__(self, action: str, parent=None, *, has_draft: bool = False, running: bool = False) -> None:
        title = {"rewind": "回退到这条消息？", "fork": "从这里创建分支？", "edit": "保存修改并回退后续内容？",
                 "regenerate": "重新执行本轮？"}[action]
        super().__init__(title, parent, width=510)
        description = {
            "rewind": "这条消息将放回输入框，等你手动发送。它及后续内容退出有效对话，原始记录保留。",
            "fork": "保留此前对话，在同一项目中创建新聊天，标题加上（分支）。这条消息放回新聊天输入框；原聊天保留。",
            "edit": "保存修改后的回答，之后的内容退出有效对话。不会自动请求模型，原始记录保留。",
            "regenerate": "重新执行整个目标，模型可能再次运行工具。原工具已产生的文件修改和命令效果不会撤销。",
        }[action]
        description += "\n\n只改变对话，项目文件保持现状。分支继续共用原项目目录。" if action == "fork" else "\n\n项目文件保持现状，不执行文件回档。"
        if running and action != "fork":
            description += "\n当前运行会先停止并保存，结算后才回退。"
        if has_draft and action in {"rewind", "regenerate"}:
            description += "\n当前未发送草稿会保留，可从会话菜单恢复。"
        label = QLabel(description)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        self.body.addWidget(label)
        self.buttons.accepted.connect(self.accept)
