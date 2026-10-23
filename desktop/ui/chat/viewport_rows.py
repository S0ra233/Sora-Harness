"""轻量消息席位只保留几何信息；正文控件由视口按需挂载。"""
import math
from PySide6.QtWidgets import QApplication, QPlainTextEdit, QTextEdit, QVBoxLayout, QWidget
from ..shell import theme


def pinned_widget(body):
    """正在编辑、聚焦或选择文字的内容不回收。"""
    focus = QApplication.focusWidget()
    if focus is not None and (focus is body or body.isAncestorOf(focus)):
        return True
    editors = [*body.findChildren(QTextEdit), *body.findChildren(QPlainTextEdit)]
    if isinstance(body, (QTextEdit, QPlainTextEdit)):
        editors.append(body)
    return getattr(body, "_editor", None) is not None or any(editor.textCursor().hasSelection() for editor in editors)


def estimated_height(row, width):
    if row.process is not None:
        return 42
    if row.reasoning or row.activity or row.page_reference:
        return 34
    chars = max(16, width // theme.BODY_FONT_SIZE)
    text = row.text
    lines = max(1, math.ceil(len(text) / chars), text.count("\n") + 1)
    if row.turn_id:
        lines = min(lines, theme.USER_COLLAPSE_LINES)
    return max(32, round(lines * theme.BODY_FONT_SIZE * theme.BODY_LINE_HEIGHT / 100) + 36 + (180 if row.images else 0))


class RowSeat(QWidget):
    def __init__(self, row, width, parent):
        super().__init__(parent)
        self.row = row
        self.measured = None
        self.body = None
        self._width = width
        self.column = QVBoxLayout(self)
        self.column.setContentsMargins(0, 0, 0, 0)
        self.column.setSpacing(0)
        self.setFixedHeight(estimated_height(row, width))

    def update_row(self, row, width):
        changed = self.row != row
        geometry_changed = changed and (row.process is None or self.row.process is None or
                                       self.row.process.rows != row.process.rows or self.row.process.running != row.process.running)
        if self.row != row:
            self.row = row
            if self.body is None:
                self.measured = None
                self._width = width
                self.setFixedHeight(estimated_height(row, width))
        return geometry_changed

    def mount(self, body):
        self.body = body
        self.setMinimumHeight(0)
        self.setMaximumHeight(16777215)
        self.column.addWidget(body)
        body.show()

    def unmount(self):
        self.measured = max(1, self.height())
        self.column.removeWidget(self.body)
        self.body = None
        self.setFixedHeight(self.measured)

    def reflow(self, width):
        if self.body is None and self._width != width:
            self._width = width
            self.measured = None
            self.setFixedHeight(estimated_height(self.row, width))
