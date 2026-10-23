"""连续的双栏阅读；段落只在视口附近创建，任务身份不成为可见卡片。"""
from dataclasses import dataclass
from math import ceil
from time import perf_counter

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPalette, QTextBlockFormat, QTextCharFormat, QTextCursor, QTextOption
from PySide6.QtWidgets import QAbstractScrollArea, QLabel, QTextEdit, QWidget

from ....ui.common.row_geometry import RowGeometry
from ....ui.common.scrolling import ScrollMotion
from ....ui.shell import theme
from .selection import ColumnSelection, reading_text


READING_FONT_SIZE = 15
READING_LINE_HEIGHT = 26
SPEAKER_COLOR = "#69798e"


@dataclass(frozen=True)
class TextPair:
    task_id: str
    source: str
    translation: str
    speaker: str = ""


def scene_pairs(snapshot, scene_id):
    """沿Core的场景顺序配对；仅添加阅读格式，不回写人物名、文本或状态。"""
    scene = next((item for item in snapshot.manifest["scenes"] if item["id"] == scene_id), None)
    if scene is None:
        return ()
    tasks = {item["id"]: item for item in snapshot.tasks}
    pairs = []
    for identifier in scene["task_ids"]:
        task = tasks[identifier]
        # 姓名任务留在Core中管理，阅读页只在对白前显示一次姓名。
        if task.get("type") == "speaker":
            continue
        speaker = task.get("speaker") or ""
        source, translation = task["text"], task.get("translation") or ""
        if speaker:
            source = f"{speaker}  [{source}]" if source else ""
            translation = f"{speaker}  [{translation}]" if translation else ""
        pairs.append(TextPair(identifier, source, translation, speaker))
    return tuple(pairs)


class _ReadingText(QTextEdit):
    def __init__(self, text, parent, reader, task_id, column, speaker):
        super().__init__(parent)
        self.reader, self.task_id, self.column = reader, task_id, column
        self.setReadOnly(True)
        self.setAcceptRichText(False)
        self.setFrameShape(QTextEdit.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setStyleSheet(f"QTextEdit {{ border: none; background: {theme.BACKGROUND}; color: {theme.TEXT}; font-size: {READING_FONT_SIZE}px; padding: 0; selection-background-color: {theme.TEXT_SELECTION}; selection-color: {theme.TEXT}; }}")
        font = self.font()
        font.setPixelSize(READING_FONT_SIZE)
        self.document().setDefaultFont(font)
        self.document().setDocumentMargin(0)
        # 只读原文，不解析HTML、Markdown或游戏文本中的指令。
        self.setPlainText(reading_text(text))
        cursor = QTextCursor(self.document())
        cursor.select(QTextCursor.SelectionType.Document)
        block = QTextBlockFormat()
        block.setLineHeight(READING_LINE_HEIGHT, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
        cursor.mergeBlockFormat(block)
        self.speaker_units = len(reading_text(speaker).encode("utf-16-le", errors="surrogatepass")) // 2 if text else 0
        if self.speaker_units:
            cursor.setPosition(0)
            cursor.setPosition(self.speaker_units, QTextCursor.MoveMode.KeepAnchor)
            name_format = QTextCharFormat()
            name_format.setForeground(QColor(SPEAKER_COLOR))
            cursor.mergeCharFormat(name_format)

    def wheelEvent(self, event):
        # 每段没有自己的滚动范围，滚轮统一交给双栏的外层视口。
        event.ignore()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.reader.selection.begin(self, event)
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            # 原生右键会移动当前段落光标，先保留同列的跨段选区。
            if not self.reader.selection.has_selection(self.column):
                self.reader.selection.begin(self, event)
                self.reader.selection.finish()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.reader.selection.begin(self, event, word=True)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        if self.reader.selection.dragging:
            self.reader.selection.extend(event.globalPosition().toPoint())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.reader.selection.dragging:
            self.reader.selection.finish()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if not self.reader.selection.shortcut(event, self.column):
            super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        self.reader.selection.context_menu(event.globalPos(), self.column)
        event.accept()


class _PairParagraph(QWidget):
    def __init__(self, pair, reader):
        super().__init__(reader.viewport())
        self.pair = pair
        self.editors = tuple(_ReadingText(text, self, reader, pair.task_id, column, pair.speaker)
                             for column, text in enumerate((pair.source, pair.translation)))
        for editor in self.editors:
            editor.selectionChanged.connect(lambda target=editor: reader.selection.native_changed(target))
            editor.cursorPositionChanged.connect(lambda target=editor: reader.selection.native_changed(target))

    def reflow(self, width, padding, gap):
        column = max(1, (width - padding * 2 - gap) // 2)
        height = 1
        for editor in self.editors:
            editor.ensurePolished()
            editor.setFixedWidth(column)
            editor.document().setTextWidth(column)
            height = max(height, ceil(editor.document().size().height()) + 2)
        for index, editor in enumerate(self.editors):
            editor.setGeometry(padding + index * (column + gap), 0, column, height)
        return height

    def pinned(self):
        return any(editor.hasFocus() or editor.textCursor().hasSelection() for editor in self.editors)


class ParallelTextView(QAbstractScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._ready = False
        self.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        palette = self.viewport().palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(theme.BACKGROUND))
        self.viewport().setPalette(palette)
        self.viewport().setAutoFillBackground(True)
        self._pairs = {}
        self._paragraphs = {}
        self._geometry = RowGeometry(10, estimate=self._estimate)
        self.selection = ColumnSelection(self)
        self._rendering = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._render_window)
        self.motion = ScrollMotion(self)
        self.verticalScrollBar().setSingleStep(24)
        self._empty = QLabel("", self.viewport())
        self._empty.setTextFormat(Qt.TextFormat.PlainText)
        self._empty.setWordWrap(True)
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        self._ready = True

    @property
    def _padding(self):
        return 24 if self.viewport().width() >= 700 else 16

    @property
    def _gap(self):
        return 40 if self.viewport().width() >= 700 else 24

    @property
    def _column_width(self):
        return max(1, (self.viewport().width() - self._padding * 2 - self._gap) // 2)

    def _estimate(self, pair, width):
        capacity = max(1, width // READING_FONT_SIZE)
        lines = max(max(1, ceil(len(text) / capacity), text.count("\n") + 1)
                    for text in (pair.source, pair.translation))
        return lines * READING_LINE_HEIGHT + 2

    def _anchor(self):
        if not self._geometry.keys:
            return None
        index = self._geometry.locate(max(0, self.verticalScrollBar().value() - 16))
        offset = self.verticalScrollBar().value() - 16 - self._geometry.offset_at(index)
        return self._geometry.keys[index], offset / max(1, self._geometry.heights[index])

    def _restore_anchor(self, anchor):
        if anchor is not None and anchor[0] in self._geometry.positions:
            key, fraction = anchor
            index = self._geometry.positions[key]
            bar = self.verticalScrollBar()
            target = max(0, min(bar.maximum(), round(16 + self._geometry.offset(key) + fraction * self._geometry.heights[index])))
            if self.motion.active:
                self.motion.shift_vertical(target - bar.value())
            else:
                bar.setValue(target)

    def set_pairs(self, pairs, *, preserve=False):
        incoming = {pair.task_id: pair for pair in pairs}
        if incoming == self._pairs and tuple(incoming) == self._geometry.keys:
            return
        anchor = self._anchor() if preserve else None
        self.selection.reconcile(incoming, preserve)
        if anchor is None or anchor[0] not in incoming:
            self.motion.stop()
            anchor = None
        for key, paragraph in tuple(self._paragraphs.items()):
            if not preserve or incoming.get(key) != paragraph.pair:
                self._release(key)
        self._pairs = incoming
        self._geometry.reset(incoming, self._column_width)
        self.selection.paint()
        self._empty.setVisible(not incoming)
        self._set_range()
        if anchor is None:
            self.verticalScrollBar().setValue(0)
        else:
            self._restore_anchor(anchor)
        self._schedule()
        self.viewport().update()

    def set_empty_text(self, text):
        self._empty.setText(text)

    def _release(self, key):
        paragraph = self._paragraphs.pop(key)
        paragraph.hide()
        paragraph.deleteLater()

    def _set_range(self):
        bar = self.verticalScrollBar()
        bar.setPageStep(self.viewport().height())
        bar.setRange(0, max(0, self._geometry.total + 32 - self.viewport().height()))

    def _schedule(self, delay=0):
        if not self._timer.isActive():
            self._timer.start(delay)

    def _scrolled(self, _value):
        self._position_paragraphs()
        self.viewport().update()
        if not self._rendering:
            self._schedule()

    def _position_paragraphs(self):
        offset = self.verticalScrollBar().value()
        for key, paragraph in self._paragraphs.items():
            paragraph.move(0, 16 + self._geometry.offset(key) - offset)

    def _ensure_paragraph(self, key):
        paragraph = self._paragraphs.get(key)
        if paragraph is None:
            paragraph = _PairParagraph(self._pairs[key], self)
            self._paragraphs[key] = paragraph
        changed = paragraph.width() != self.viewport().width() or not paragraph.isVisible()
        if changed:
            measured = paragraph.reflow(self.viewport().width(), self._padding, self._gap)
            paragraph.resize(self.viewport().width(), measured)
            self._geometry.update(key, measured)
            paragraph.show()
            for column, editor in enumerate(paragraph.editors):
                self.selection.paint_editor(editor, key, column)
        return paragraph, changed

    def _render_window(self):
        if not self.isVisible() or not self._pairs:
            return
        self._rendering = True
        try:
            height = self.viewport().height()
            top = self.verticalScrollBar().value()
            anchor = self._anchor()
            first = self._geometry.locate(max(0, top - height - 16))
            last = self._geometry.locate(top + height * 2)
            visible = set(self._geometry.keys[first:last + 1])
            for key, paragraph in tuple(self._paragraphs.items()):
                if key not in visible and not paragraph.pinned():
                    self._release(key)
            started = perf_counter()
            pending = False
            current_first = self._geometry.locate(max(0, top - 16))
            current_last = self._geometry.locate(top + height)
            # 先补当前屏，再预加载邻屏，快速拖动时不先把预算花在屏外。
            ordered = (*self._geometry.keys[current_first:current_last + 1],
                       *reversed(self._geometry.keys[first:current_first]),
                       *self._geometry.keys[current_last + 1:last + 1])
            for key in ordered:
                self._ensure_paragraph(key)
                if perf_counter() - started >= .006:
                    pending = True
                    break
            self._set_range()
            self._restore_anchor(anchor)
            self._position_paragraphs()
            self.viewport().update()
            if pending:
                self._schedule(16)
            else:
                # 测量会改变视口对应的行区间；下一帧补齐新进入的段落。
                actual = self._geometry.locate(self.verticalScrollBar().value() + height)
                if self._geometry.keys[actual] not in self._paragraphs:
                    self._schedule(16)
        finally:
            self._rendering = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not getattr(self, "_ready", False):
            return
        anchor = self._anchor()
        self._geometry.reset(self._pairs, self._column_width)
        self._set_range()
        self._restore_anchor(anchor)
        self._empty.setGeometry(24, 24, max(0, self.viewport().width() - 48), max(0, self.viewport().height() - 48))
        self._schedule()

    def scrollContentsBy(self, dx, dy):
        if getattr(self, "_ready", False):
            self._scrolled(self.verticalScrollBar().value())

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule()

    def hideEvent(self, event):
        self._timer.stop()
        self.motion.stop()
        self.selection.hide()
        super().hideEvent(event)
