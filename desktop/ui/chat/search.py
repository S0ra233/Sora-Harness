"""当前正文的字面搜索；用 Qt 文档位置处理中文和 Unicode，不读日志。"""
from time import perf_counter
from PySide6.QtCore import QEasingCurve, Property, QPropertyAnimation, QRectF, Qt, QTimer
from PySide6.QtGui import QKeySequence, QPainter, QShortcut, QTextCursor
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QWidget

from ..shell import theme
from .transcript import Transcript
from ..common.widgets import IconButton, IconGlyph
from ..common.surfaces import FadeVeil, paint_card


class TranscriptSearch(QWidget):
    def __init__(self, transcript: Transcript, parent=None) -> None:
        super().__init__(parent)
        self._transcript = transcript
        self._matches = []
        self._documents = None
        self._current = None
        self._scan = QTimer(self)
        self._scan.setSingleShot(True)
        self._scan.timeout.connect(self._scan_batch)
        self._index = -1
        self._open = False
        self._reveal = 0.0
        self._animation = QPropertyAnimation(self, b"reveal", self)
        self._animation.setDuration(120)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.finished.connect(self._finish_animation)
        self.setFixedHeight(52)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 9, 12, 9)
        layout.setSpacing(8)
        layout.addWidget(IconGlyph("search", size=16))
        self._input = QLineEdit()
        self._input.setPlaceholderText("搜索当前对话")
        self._input.setMaximumWidth(320)
        self._count = QLabel("0 / 0")
        self._count.setStyleSheet(f"color: {theme.MUTED}; font-size: 12px;")
        layout.addWidget(self._input, 1)
        previous = IconButton("up", "上一处命中", size=27)
        following = IconButton("down", "下一处命中", size=27)
        close = IconButton("close", "关闭正文搜索", size=27)
        for widget in (self._count, previous, following, close):
            layout.addWidget(widget)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._find)
        self._input.textChanged.connect(self._query_changed)
        transcript.changed.connect(self._schedule)
        previous.clicked.connect(lambda: self._move(-1))
        following.clicked.connect(lambda: self._move(1))
        self._input.returnPressed.connect(lambda: self._move(1))
        close.clicked.connect(self.close_search)
        escape = QShortcut(QKeySequence("Esc"), self._input)
        escape.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        escape.activated.connect(self.close_search)
        self._veil = FadeVeil(self)
        self._veil.opacity = 1.0
        self.hide()

    @Property(float)
    def reveal(self) -> float:
        return self._reveal

    @reveal.setter
    def reveal(self, value: float) -> None:
        self._reveal = value
        self._veil.opacity = 1 - value
        self.place()
        self.update()

    def place(self) -> None:
        if self.parentWidget() is not None:
            width = min(520, max(0, self.parentWidget().width() - 64))
            self.setFixedWidth(width)
            self.move(max(16, self.parentWidget().width() - width - 20), round(10 + (1 - self._reveal) * 10))
        self._veil.setGeometry(self.rect())
        self._veil.raise_()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setOpacity(self._reveal)
            paint_card(painter, QRectF(self.rect()).adjusted(5, 5, -5, -5), radius=12)
        finally:
            painter.end()

    def _finish_animation(self) -> None:
        if not self._open:
            self.hide()

    def hideEvent(self, event) -> None:
        self._animation.stop()
        self._cancel_scan()
        super().hideEvent(event)

    def open_search(self) -> None:
        self._open = True
        self.place()
        self.show()
        self.raise_()
        self._animate(1.0)
        self._input.setFocus()
        self._input.selectAll()
        self._schedule()

    def close_search(self) -> None:
        self._open = False
        self._input.clear()
        self._timer.stop()
        self._find()
        self._animate(0.0)

    def _animate(self, target: float) -> None:
        self._animation.stop()
        self._veil.show()
        self._animation.setStartValue(self._reveal)
        self._animation.setEndValue(target)
        self._animation.start()

    def _schedule(self) -> None:
        self._cancel_scan()
        self._transcript.clear_highlights()
        self._matches = []
        self._index = -1
        if self._open and self._input.text():
            if not self._timer.isActive():
                self._timer.start()

    def _query_changed(self, _text):
        self._cancel_scan()
        self._transcript.clear_highlights()
        self._matches, self._index = [], -1
        self._timer.start()

    def _cancel_scan(self):
        self._scan.stop()
        if self._documents is not None:
            self._documents.close()
        self._documents = self._current = None

    def _find(self) -> None:
        self._cancel_scan()
        query = self._input.text()
        self._matches = []
        self._transcript.clear_highlights()
        self._index = -1
        if not query:
            self._transcript.clear_highlights()
            self._index = -1
            self._count.setText("0 / 0")
            return
        self._documents = self._transcript.documents()
        self._count.setText("查找中…")
        self._scan.start(0)

    def _scan_batch(self):
        query = self._input.text()
        deadline = perf_counter() + .006
        while self._documents is not None:
            if self._current is None:
                try:
                    key, call_id, document = next(self._documents)
                except StopIteration:
                    self._documents = None
                    break
                self._current = (key, call_id, document, QTextCursor(document), [])
            key, call_id, document, cursor, ranges = self._current
            cursor = document.find(query, cursor)
            if cursor.isNull():
                self._transcript.set_highlights(key, call_id, tuple(ranges))
                self._current = None
            else:
                start, end = cursor.selectionStart(), cursor.selectionEnd()
                ranges.append((start, end))
                self._matches.append((key, call_id, start, end))
                self._current = (key, call_id, document, cursor, ranges)
            if perf_counter() >= deadline:
                break
        count = f"{self._index + 1} / {len(self._matches)}"
        self._count.setText(count + (" · 查找中" if self._documents is not None else ""))
        if self._documents is not None:
            self._scan.start(16)

    def _move(self, direction: int) -> None:
        if self._timer.isActive():
            self._timer.stop()
            self._find()
        if self._matches:
            if self._index < 0:
                self._index = 0 if direction > 0 else len(self._matches) - 1
            else:
                self._index = (self._index + direction) % len(self._matches)
            key, call_id, start, end = self._matches[self._index]
            self._transcript.reveal(key, call_id, start, end)
            self._count.setText(f"{self._index + 1} / {len(self._matches)}")
