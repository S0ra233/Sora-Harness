"""公开思考的紧凑抽屉；只有实际文本才创建详情，隐藏思考只显示状态。"""
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget
from PySide6.QtCore import Signal

from ..common.disclosure import AnimatedDisclosure
from ..common.widgets import highlight_text
from .tools.group import _Header


class ReasoningCard(QWidget):
    interacted = Signal()
    layout_changed = Signal()

    def __init__(self, row, state, text_factory, parent=None) -> None:
        super().__init__(parent)
        self._state, self._factory = state, text_factory
        self._row = None
        self.editor = None
        self._disclosure = None
        self._ranges = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.header = _Header()
        self.header.setMinimumHeight(24)
        self.header.layout().setContentsMargins(0, 0, 4, 0)
        self.header.toggled.connect(self._toggle)
        layout.addWidget(self.header)
        self.update_row(row)

    def _ensure_body(self) -> None:
        if self.editor is not None:
            return
        card = QFrame()
        card.setObjectName("reasoningCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 8, 12, 8)
        self.editor = self._factory(compact=True)
        self.editor.set_content(self._row.text, not self._row.reasoning_running)
        highlight_text(self.editor, self._ranges)
        self.editor.interacted.connect(self.interacted.emit)
        layout.addWidget(self.editor)
        self._disclosure = AnimatedDisclosure(card)
        self._disclosure.geometry_changed.connect(self.layout_changed.emit)
        self.editor.geometry_changed.connect(self._disclosure.refresh_height)
        self.layout().addWidget(self._disclosure)

    def update_row(self, row) -> None:
        self._row = row
        has_text = any(not char.isspace() for char in row.text)
        preview = row.text[:180].split("\n", 1)[0].replace("**", "").strip()
        title = ("思考摘要" if row.reasoning_kind == "summary" else "思考") + (" · " + preview if preview else "")
        if not has_text:
            title = "模型思考中" if row.reasoning_running else "思考完成（内容未公开）"
        self.header.set_summary(title, "think", row.reasoning_running)
        self.header._arrow.setVisible(has_text)
        self.header.setEnabled(has_text)
        expanded = has_text and (self._state.reasoning_expanded if self._state.reasoning_expanded is not None else row.reasoning_running)
        self.header.blockSignals(True)
        self.header.setChecked(expanded)
        self.header.blockSignals(False)
        if expanded or self.editor is not None:
            self._ensure_body()
            self.editor.set_content(row.text, not row.reasoning_running)
            self._disclosure.set_expanded(expanded)
        self.layout_changed.emit()

    def _toggle(self, expanded: bool) -> None:
        self._state.reasoning_expanded = expanded
        if expanded:
            self._ensure_body()
            self.editor.set_content(self._row.text, not self._row.reasoning_running)
        if self._disclosure is not None:
            self._disclosure.set_expanded(expanded)
        self.interacted.emit()

    def document(self):
        self._ensure_body()
        self.editor.set_content(self._row.text, not self._row.reasoning_running)
        return self.editor.document()

    def set_highlights(self, ranges):
        self._ranges = ranges
        if self.editor is not None:
            highlight_text(self.editor, ranges)

    def reveal(self):
        self.header.setChecked(True)
        self._disclosure.finish_animation()
        return self.editor
