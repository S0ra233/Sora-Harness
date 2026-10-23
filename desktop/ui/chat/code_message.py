"""回复中的独立代码卡片；流式增量追加，正文完成后才解析 Markdown。"""
from dataclasses import dataclass
import re
import math
from time import perf_counter
from collections import OrderedDict

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QTextBlockFormat, QTextCursor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from ..common.read_only_text import ReadOnlyText
from ..common.widgets import highlight_text
from ..shell import theme
from .code_highlight import CodeHighlighter
from .viewport_rows import pinned_widget


@dataclass(frozen=True)
class MessagePart:
    key: str
    text: str
    language: str | None = None
    closed: bool = True


def split_fenced_code(text: str) -> tuple[MessagePart, ...]:
    """只抽取顶层围栏；列表和引用内的 Markdown 继续交给 Qt 解析。"""
    parts, prose = [], []
    lines = text.splitlines(keepends=True)
    index = 0
    while index < len(lines):
        opening = re.fullmatch(r"(`{3,}|~{3,})([^\r\n]*)[\r\n]*", lines[index])
        if opening is None or (opening[1][0] == "`" and "`" in opening[2]):
            prose.append(lines[index])
            index += 1
            continue
        if prose:
            parts.append(MessagePart(f"body:{len(parts)}", "".join(prose)))
            prose = []
        fence, language, code = opening[1], opening[2].strip(), []
        index += 1
        closing = re.compile(r" {0,3}" + re.escape(fence[0]) + r"{" + str(len(fence)) + r",}[ \t]*[\r\n]*")
        while index < len(lines) and closing.fullmatch(lines[index]) is None:
            code.append(lines[index])
            index += 1
        parts.append(MessagePart(f"body:{len(parts)}", "".join(code), language or "代码", index < len(lines)))
        if index < len(lines):
            index += 1
    if prose:
        parts.append(MessagePart(f"body:{len(parts)}", "".join(prose)))
    return tuple(parts)


def display_parts(text: str) -> tuple[MessagePart, ...]:
    """长正文只在独立段落边界拆分；列表、表格和引用保持一个完整单元。"""
    output = []
    references = "\n".join(re.findall(r"^ {0,3}\[[^\]\n]+\]:[^\n]*(?:\n[ \t]+[^\n]+)*", text, re.M))
    protected = re.compile(r"(?m)^(?:[ \t]|>|[-+*] |\d+[.)] |\||```|~~~|\[[^\]]+\]:)|\|.*\||^ {0,3}(?:[-*_] *){3,}$")
    for part in split_fenced_code(text):
        if part.language is not None or len(part.text) <= 6000 or re.search(r"(?m)^ {0,3}\[\^", part.text):
            output.append(part)
            continue
        chunks, pending, size, previous = [], [], 0, ""
        for paragraph in re.split(r"(?<=\n)\n+", part.text):
            # 不在连续列表项或表格中拆开，避免改变标记、缩进与引用含义。
            balanced = all(len(re.findall(pattern, previous)) % 2 == 0 for pattern in (r"(?<!\\)\*\*", r"(?<!\\)`", r"(?<!\\)_"))
            if size >= 4000 and pending and balanced and not protected.search(previous) and not protected.search(paragraph):
                chunks.append("\n\n".join(pending))
                pending, size = [], 0
            pending.append(paragraph)
            size += len(paragraph)
            previous = paragraph
        if pending:
            chunks.append("\n\n".join(pending))
        if len(chunks) <= 1:
            output.append(part)
        else:
            output.extend(MessagePart(f"{part.key}:paragraph:{index}", chunk + ("\n\n" + references if references else ""))
                          for index, chunk in enumerate(chunks))
    return tuple(output)


class FencePartition:
    """冻结已结束片段，流式时只检查新增完整行；结束后统一按原解析器定稿。"""

    def __init__(self) -> None:
        self._source = ""
        self._stable: list[MessagePart] = []
        self._body: list[str] = []
        self._pending = ""
        self._closing = None
        self._language: str | None = None
        self._settled = False
        self._parts: tuple[MessagePart, ...] = ()

    def update(self, text: str, settled: bool) -> tuple[MessagePart, ...]:
        if text == self._source and settled == self._settled:
            return self._parts
        if self._settled or not text.startswith(self._source):
            self.__init__()
        delta, self._source = text[len(self._source):], text
        if settled:
            self._parts = display_parts(text)
            self._settled = True
            return self._parts
        for line in delta.splitlines(keepends=True):
            line, self._pending = self._pending + line, ""
            if not line.endswith("\n"):
                self._pending = line
                continue
            if self._closing is not None:
                if self._closing.fullmatch(line):
                    self._freeze(closed=True)
                    self._closing, self._language = None, None
                else:
                    self._body.append(line)
                continue
            opening = re.fullmatch(r"(`{3,}|~{3,})([^\r\n]*)[\r\n]*", line)
            if opening is None or (opening[1][0] == "`" and "`" in opening[2]):
                self._body.append(line)
            else:
                if self._body:
                    self._freeze(closed=True)
                fence = opening[1]
                self._language = opening[2].strip() or "代码"
                self._closing = re.compile(r" {0,3}" + re.escape(fence[0]) + r"{" + str(len(fence)) + r",}[ \t]*[\r\n]*")
        active = "".join(self._body) + self._pending
        tail = (MessagePart(f"body:{len(self._stable)}", active, self._language, self._language is None),) if active or self._closing is not None else ()
        self._parts = (*self._stable, *tail)
        return self._parts

    def _freeze(self, *, closed: bool) -> None:
        self._stable.append(MessagePart(f"body:{len(self._stable)}", "".join(self._body), self._language, closed))
        self._body.clear()


class CodeCard(QFrame):
    interacted = Signal()
    layout_changed = Signal()

    def __init__(self, part: MessagePart, position: tuple[int, int], parent=None, *, settled: bool = True) -> None:
        super().__init__(parent)
        self.setObjectName("codeCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(13, 9, 13, 12)
        layout.setSpacing(7)
        header = QHBoxLayout()
        label = QLabel(part.language)
        self._label = label
        label.setTextFormat(Qt.TextFormat.PlainText)
        header.addWidget(label, 1)
        layout.addLayout(header)
        self.editor = ReadOnlyText()
        self.editor.setFrameShape(QFrame.Shape.NoFrame)
        self.editor.setPlainText(part.text)
        self._syntax = CodeHighlighter(self.editor.document())
        self._format_code(part, settled)
        self._body = part.text
        self._fit_code()
        layout.addWidget(self.editor)
        self._pending = position
        self._restore = QTimer(self)
        self._restore.setSingleShot(True)
        self._restore.timeout.connect(self._restore_position)
        self.editor.interacted.connect(self._interact)
        for bar in (self.editor.verticalScrollBar(), self.editor.horizontalScrollBar()):
            bar.sliderPressed.connect(self._interact)
            bar.actionTriggered.connect(self._interact)

    def set_part(self, part: MessagePart, *, settled: bool = True) -> None:
        self._label.setText(part.language)
        if part.text == self._body:
            self._syntax.set_source(part.text, part.language or "text", settled=part.closed or settled)
            return
        position = self.position()
        appended = part.text.startswith(self._body)
        if appended:
            cursor = QTextCursor(self.editor.document())
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText(part.text[len(self._body):])
        else:
            self.editor.setPlainText(part.text)
        self._body = part.text
        if appended:
            # 新段落继承已有20px行高，增量片段不重新格式化整份文档。
            self._syntax.set_source(part.text, part.language or "text", settled=part.closed or settled)
        else:
            self._format_code(part, settled)
        self._fit_code()
        self._pending = position
        self._restore.start(0)

    def _format_code(self, part: MessagePart, settled: bool) -> None:
        cursor = QTextCursor(self.editor.document())
        cursor.select(QTextCursor.SelectionType.Document)
        spacing = QTextBlockFormat()
        spacing.setLineHeight(20, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        cursor.mergeBlockFormat(spacing)
        self._syntax.set_source(part.text, part.language or "text", settled=part.closed or settled)

    def _fit_code(self) -> None:
        height = min(theme.CODE_MAX_HEIGHT, max(42, self.editor.blockCount() * 20 + 24))
        if height != self.editor.height():
            self.editor.setFixedHeight(height)
            self.layout_changed.emit()

    def _interact(self, *_args) -> None:
        self._restore.stop()
        self._pending = None
        self.interacted.emit()

    def position(self) -> tuple[int, int]:
        return self._pending if self._pending is not None else (
            self.editor.verticalScrollBar().value(), self.editor.horizontalScrollBar().value())

    def _restore_position(self) -> None:
        if self._pending is not None and self.isVisible():
            vertical, horizontal = self._pending
            self.editor.verticalScrollBar().setValue(vertical)
            self.editor.horizontalScrollBar().setValue(horizontal)
            self._pending = None

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
        self._restore.start(0)


class _PartSeat(QWidget):
    def __init__(self, part, parent):
        super().__init__(parent)
        self.part, self.body = part, None
        self._width = None
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(0)
        self.estimate()

    def estimate(self):
        if self.body is not None:
            return
        width = max(200, self.parentWidget().width())
        if width == self._width:
            return
        self._width = width
        lines = max(1, self.part.text.count("\n") + 1, math.ceil(len(self.part.text) / max(16, width // 13)))
        height = min(theme.CODE_MAX_HEIGHT, lines * 20 + 65) if self.part.language is not None else lines * 22
        self.setFixedHeight(max(24, height))

    def mount(self, body):
        self.body = body
        self.setMinimumHeight(0)
        self.setMaximumHeight(16777215)
        self.box.addWidget(body)
        self.box.activate()
        body.show()

    def unmount(self):
        height = self.height()
        self.box.removeWidget(self.body)
        self.body = None
        self.setFixedHeight(max(1, height))


class CodeMessage(QWidget):
    interacted = Signal()
    layout_changed = Signal()

    def __init__(self, parts, text_factory, positions, parent=None, *, markdown: bool = True) -> None:
        super().__init__(parent)
        self._editors, self._cards, self._widgets = {}, {}, {}
        self._seats, self._parts = {}, {}
        self._ranges = {}
        self._recent = OrderedDict()
        self._area = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._render_window)
        self._text_factory, self._saved_positions = text_factory, positions
        self._layout = layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.set_parts(parts, markdown=markdown)

    def set_parts(self, parts, *, markdown: bool) -> None:
        self._markdown = markdown
        wanted = {part.key: part for part in parts}
        for key in tuple(self._seats):
            if key not in wanted or self._seats[key].part.language != wanted[key].language:
                if key in self._cards:
                    self._saved_positions[key] = self._cards.pop(key).position()
                self._widgets.pop(key, None)
                self._editors.pop(key, None)
                self._recent.pop(key, None)
                seat = self._seats.pop(key)
                self._layout.removeWidget(seat)
                seat.hide()
                seat.deleteLater()
        for index, part in enumerate(parts):
            seat = self._seats.get(part.key)
            if seat is None:
                seat = self._seats[part.key] = _PartSeat(part, self)
                self._layout.insertWidget(index, seat)
            else:
                if seat.part != part:
                    seat._width = None
                seat.part = part
                seat.estimate()
                if self._layout.indexOf(seat) != index:
                    self._layout.insertWidget(index, seat)
            widget = self._widgets.get(part.key)
            if widget is not None and part.language is not None:
                widget.set_part(part, settled=markdown)
            elif widget is not None:
                widget.set_content(part.text, markdown)
        self._parts = wanted
        self._schedule()

    def _ensure_part(self, key):
        if key in self._widgets:
            return self._widgets[key]
        part = self._parts[key]
        if part.language is not None:
            widget = CodeCard(part, self._saved_positions.get(key, (0, 0)), settled=self._markdown)
            self._cards[key] = widget
            editor = widget.editor
            widget.layout_changed.connect(self.layout_changed.emit)
        else:
            widget = editor = self._text_factory()
            editor.set_content(part.text, self._markdown)
            editor.geometry_changed.connect(self.layout_changed.emit)
        widget.interacted.connect(self.interacted.emit)
        self._widgets[key], self._editors[key] = widget, editor
        self._recent[key] = None
        self._seats[key].mount(widget)
        highlight_text(editor, self._ranges.get(key, ()))
        return widget

    def _schedule(self, *_args):
        if self.isVisible() and not self._timer.isActive():
            self._timer.start(16)

    def _render_window(self):
        if not self.isVisible() or self._area is None:
            return
        viewport = self._area.viewport()
        self._layout.activate()
        height = viewport.height()
        candidates = [(key, seat.mapTo(viewport, seat.rect().topLeft()).y()) for key, seat in self._seats.items()]
        candidates = [(key, y) for key, y in candidates if y < height * 3 and y + self._seats[key].height() > -height * 2]
        deadline, changed = perf_counter() + .006, False
        for key, _y in sorted(candidates, key=lambda item: abs(item[1])):
            if key not in self._widgets:
                self._ensure_part(key)
                changed = True
                if perf_counter() >= deadline:
                    break
            self._recent.move_to_end(key)
        wanted = {key for key, _y in candidates}
        for key in tuple(self._recent):
            if len(self._widgets) <= max(20, len(wanted)):
                break
            if key not in wanted and not pinned_widget(self._widgets[key]):
                widget = self._widgets.pop(key)
                if key in self._cards:
                    self._saved_positions[key] = self._cards.pop(key).position()
                self._editors.pop(key)
                self._recent.pop(key)
                self._seats[key].unmount()
                widget.hide()
                widget.deleteLater()
        if changed:
            self._layout.activate()
            self.layout_changed.emit()
        if any(key not in self._widgets for key, _y in candidates):
            self._schedule()

    def reading_anchor(self, viewport):
        for key in self._parts:
            seat = self._seats[key]
            y = seat.mapTo(viewport, seat.rect().topLeft()).y()
            if y + seat.height() > 0:
                return key, y
        return None

    def anchor_widget(self, key):
        return self._seats.get(key)

    def showEvent(self, event):
        super().showEvent(event)
        if self._area is None:
            parent = self.parentWidget()
            while parent is not None:
                if isinstance(parent, QScrollArea) and parent.objectName() == "transcriptScroll":
                    self._area = parent
                    parent.verticalScrollBar().valueChanged.connect(self._schedule)
                    parent.verticalScrollBar().rangeChanged.connect(self._schedule)
                    break
                parent = parent.parentWidget()
        self._schedule()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        for seat in self._seats.values():
            seat.estimate()
        self._schedule()

    def positions(self) -> dict[str, tuple[int, int]]:
        return {**self._saved_positions, **{key: card.position() for key, card in self._cards.items()}}

    def documents(self):
        return tuple((key, editor.document()) for key, editor in self._editors.items())

    def set_highlights(self, key, ranges) -> None:
        self._ranges[key] = ranges
        if key in self._editors:
            highlight_text(self._editors[key], ranges)

    def clear_highlights(self) -> None:
        self._ranges.clear()
        for editor in self._editors.values():
            highlight_text(editor, ())

    def reveal(self, key: str, start: int, end: int):
        self._ensure_part(key)
        self._recent.move_to_end(key)
        if key in self._cards:
            return self._cards[key].reveal(start, end)
        self.interacted.emit()
        editor = self._editors[key]
        cursor = QTextCursor(editor.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        editor.setTextCursor(cursor)
        editor.ensureCursorVisible()
        return editor, cursor
