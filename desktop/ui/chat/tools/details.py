"""有界高度的只读详情；自己消费滚轮，不重新执行工具或读取日志。"""
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

from ...common.widgets import highlight_text
from ...common.read_only_text import ReadOnlyText as _Output
from ...shell import theme
from .models import ToolItemState, ToolPresentation


class _DiffHighlighter(QSyntaxHighlighter):
    """只着色实际返回的差异文本，不更改内容或搜索字符位置。"""

    def __init__(self, document) -> None:
        super().__init__(document)
        self.enabled = False

    def highlightBlock(self, text: str) -> None:
        if not self.enabled:
            return
        style = QTextCharFormat()
        if text.startswith("+") and not text.startswith("+++"):
            style.setBackground(QColor("#e7f5ea"))
            style.setForeground(QColor(theme.DIFF_ADDED))
        elif text.startswith("-") and not text.startswith("---"):
            style.setBackground(QColor("#fde8e7"))
            style.setForeground(QColor(theme.DIFF_REMOVED))
        elif text.startswith("@@"):
            style.setForeground(QColor(theme.MUTED))
        else:
            return
        self.setFormat(0, len(text.encode("utf-16-le")) // 2, style)


class ToolDetails(QFrame):
    interacted = Signal()
    layout_changed = Signal()

    def __init__(self, presentation: ToolPresentation, state: ToolItemState, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("toolDetails")
        self._state = state
        self._generation = 0
        self._pending_scroll: tuple[int, int] | None = (state.vertical, state.horizontal)
        self._restore_generation = 0
        self._restore_timer = QTimer(self)
        self._restore_timer.setSingleShot(True)
        self._restore_timer.timeout.connect(self._restore_scroll)
        self._height_limit = theme.TOOL_OUTPUT_MAX_HEIGHT
        self._body: str | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(13, 10, 13, 10)
        layout.setSpacing(9)
        self._heading = QLabel()
        self._heading.setObjectName("toolDetailsHeading")
        self._heading.setTextFormat(Qt.TextFormat.PlainText)
        self.editor = _Output()
        self.editor.setObjectName("toolOutput")
        self.editor.setFrameShape(QFrame.Shape.NoFrame)
        self._diff = _DiffHighlighter(self.editor.document())
        self._footer = QLabel()
        self._footer.setObjectName("toolDetailsFooter")
        self._footer.setTextFormat(Qt.TextFormat.PlainText)
        self._footer.setWordWrap(True)
        layout.addWidget(self._heading)
        layout.addWidget(self.editor)
        layout.addWidget(self._footer)
        self.editor.interacted.connect(self._interact)
        for bar in (self.editor.verticalScrollBar(), self.editor.horizontalScrollBar()):
            bar.sliderPressed.connect(self._interact)
            bar.actionTriggered.connect(self._interact)
        self.set_presentation(presentation)

    def _interact(self, *_args) -> None:
        self._generation += 1
        self._restore_timer.stop()
        self._pending_scroll = None
        self.interacted.emit()

    def save_state(self) -> None:
        position = self._pending_scroll or (self.editor.verticalScrollBar().value(), self.editor.horizontalScrollBar().value())
        self._state.vertical, self._state.horizontal = position

    def set_presentation(self, presentation: ToolPresentation) -> None:
        self._heading.setText(presentation.heading)
        self._footer.setText(presentation.footer)
        diff_enabled = presentation.diff_highlight
        changed_diff = diff_enabled != self._diff.enabled
        self._diff.enabled = diff_enabled
        if presentation.body != self._body:
            self.save_state()
            self._pending_scroll = (self._state.vertical, self._state.horizontal)
            self._body = presentation.body
            self.editor.setPlainText(presentation.body)
            self._fit_output()
            self._schedule_restore()
        elif changed_diff:
            self._diff.rehighlight()

    def set_height_limit(self, height: int) -> None:
        self._height_limit = height
        self._fit_output()

    def _fit_output(self) -> None:
        # NoWrap下文档行数就是显示行数；留出横向滚动条，不按全文扩大窗口。
        natural = self.editor.blockCount() * self.editor.fontMetrics().lineSpacing() + 26
        height = min(max(60, self._height_limit), max(60, natural))
        if self.editor.height() != height:
            self.editor.setFixedHeight(height)
            self.layout_changed.emit()

    def _schedule_restore(self) -> None:
        self._restore_generation = self._generation
        self._restore_timer.start(0)

    def _restore_scroll(self) -> None:
        if not self.isVisible() or self._restore_generation != self._generation or self._pending_scroll is None:
            return
        vertical, horizontal = self._pending_scroll
        self.editor.verticalScrollBar().setValue(vertical)
        self.editor.horizontalScrollBar().setValue(horizontal)
        self._pending_scroll = None

    def set_highlights(self, ranges: tuple[tuple[int, int], ...]) -> None:
        highlight_text(self.editor, ranges)

    def reveal(self, start: int, end: int):
        self._interact()
        cursor = QTextCursor(self.editor.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        self.editor.ensureCursorVisible()
        return self.editor, cursor

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fit_output()
        self._schedule_restore()

    def hideEvent(self, event) -> None:
        self.save_state()
        self._restore_timer.stop()
        self._pending_scroll = (self._state.vertical, self._state.horizontal)
        super().hideEvent(event)
