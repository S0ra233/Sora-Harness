"""欢迎页的用户／AI预填气泡；只编辑配置，不生成真正会话或调用模型。"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget
from ..common.buttons import MotionButton
from ..common.widgets import IconButton
from ..common.scrolling import SmoothScrollArea
from ..common.disclosure import AnimatedDisclosure
from ..settings.prompt_page import PromptText
from ..shell import theme


class PreludeEditor(QWidget):
    changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pairs, self._loading = [], False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 6)
        self._toggle = MotionButton("预填用户／AI对话", variant="ghost")
        self._toggle.setCheckable(True)
        layout.addWidget(self._toggle, 0, Qt.AlignmentFlag.AlignLeft)
        content = QWidget()
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        note = QLabel("作为前置示例保存，不会自动请求模型；每组用户／AI都需填写。")
        note.setStyleSheet(f"color: {theme.MUTED}; font-size: 11px;")
        note.setWordWrap(True)
        column.addWidget(note)
        self._body = QWidget()
        self._rows = QVBoxLayout(self._body)
        self._rows.setContentsMargins(0, 0, 0, 0)
        self._rows.setSpacing(8)
        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(220)
        scroll.setMinimumHeight(150)
        scroll.setWidget(self._body)
        scroll.setStyleSheet("QScrollArea { background: white; border: none; }")
        column.addWidget(scroll)
        add = MotionButton("添加一组", variant="secondary")
        add.clicked.connect(lambda: self._add("", ""))
        column.addWidget(add, 0, Qt.AlignmentFlag.AlignLeft)
        self._disclosure = AnimatedDisclosure(content)
        self._toggle.toggled.connect(self._disclosure.set_expanded)
        layout.addWidget(self._disclosure)

    def _add(self, user, assistant) -> None:
        pair = QWidget()
        layout = QVBoxLayout(pair)
        layout.setContentsMargins(0, 0, 0, 0)
        editors = []
        for role, text in (("用户", user), ("AI", assistant)):
            row = QHBoxLayout()
            label = QLabel(role)
            label.setStyleSheet(f"color: {theme.MUTED}; font-size: 11px;")
            editor = PromptText(text)
            editor.setFixedHeight(70)
            color = theme.USER_MESSAGE
            editor.setStyleSheet(f"background: {color if role == '用户' else theme.CONTROL}; color: {theme.TEXT}; border: none; border-radius: 14px; padding: 10px;")
            editor.textChanged.connect(self._changed)
            row.addWidget(label)
            row.addWidget(editor, 1)
            layout.addLayout(row)
            editors.append(editor)
        remove = IconButton("close", "移除这组预填对话")
        remove.clicked.connect(lambda: self._remove(pair))
        layout.addWidget(remove, 0, Qt.AlignmentFlag.AlignRight)
        self._pairs.append((pair, *editors))
        self._rows.addWidget(pair)
        self._changed()

    def _remove(self, widget) -> None:
        self._pairs = [pair for pair in self._pairs if pair[0] is not widget]
        self._rows.removeWidget(widget)
        widget.hide()
        widget.deleteLater()
        self._changed()

    def _changed(self) -> None:
        if not self._loading:
            self.changed.emit()

    def values(self):
        return [message for _, user, assistant in self._pairs for message in (
            {"role": "user", "content": user.toPlainText()}, {"role": "assistant", "content": assistant.toPlainText()})]

    def restore(self, values) -> None:
        self._loading = True
        for pair, _, _ in self._pairs:
            self._rows.removeWidget(pair)
            pair.hide()
            pair.deleteLater()
        self._pairs = []
        for i in range(0, len(values), 2):
            self._add(values[i]["content"], values[i + 1]["content"])
        self._loading = False
