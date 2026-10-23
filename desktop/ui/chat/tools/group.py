"""连续工具过程的汇总和明细；按调用ID复用控件与阅读位置。"""
from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QTextDocument
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from ...common.icons import icon
from ...common.disclosure import AnimatedDisclosure
from ...common.status_text import StatusText as _StatusText
from ...common.scrolling import SmoothScrollArea
from ...common.motion import HoverMotion
from ...shell import theme
from .details import ToolDetails
from .models import ToolGroupState, ToolItemState, ToolView, group_summary, present_tool


class _Header(QToolButton):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("toolHeader")
        self._motion = HoverMotion(self)
        self.setStyleSheet("QToolButton#toolHeader, QToolButton#toolHeader:hover, QToolButton#toolHeader:focus { background: transparent; border: none; }")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(24)
        self._icon_name = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(8)
        self._glyph = QLabel()
        self._glyph.setFixedSize(16, 16)
        self._status_text = _StatusText()
        self._annotation = QLabel()
        self._annotation.setObjectName("toolAnnotation")
        self._annotation.setTextFormat(Qt.TextFormat.RichText)
        self._arrow = QLabel()
        self._arrow.setFixedSize(12, 12)
        for label in (self._glyph, self._annotation, self._arrow):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self._glyph)
        layout.addWidget(self._status_text, 1)
        layout.addWidget(self._annotation)
        layout.addWidget(self._arrow)
        self.toggled.connect(self._set_arrow)
        self._set_arrow(False)

    def _set_arrow(self, expanded: bool) -> None:
        self._arrow.setPixmap(icon("chevron" if expanded else "right", theme.TOOL_TEXT, 12).pixmap(QSize(12, 12)))

    def set_summary(self, title: str, glyph: str, running: bool, annotation: str = "") -> None:
        self._status_text.set_status(title, running)
        self.setToolTip(title)
        self.setAccessibleName(title)
        pieces = annotation.split()
        self._annotation.setText(
            f'<span style="color:{theme.DIFF_ADDED}">{pieces[0]}</span> <span style="color:{theme.DIFF_REMOVED}">{pieces[1]}</span>'
            if len(pieces) == 2 and pieces[0].startswith("+") and pieces[1].startswith("−") else "")
        self._annotation.setVisible(bool(annotation))
        if glyph != self._icon_name:
            self._glyph.setPixmap(icon(glyph, theme.TOOL_TEXT, 16).pixmap(QSize(16, 16)))
            self._icon_name = glyph

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            color = QColor(theme.PRESSED if self.isDown() else theme.HOVER)
            color.setAlphaF(1.0 if self.isDown() else self._motion.progress)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(self.rect()), 9, 9)
            if self._motion.keyboard_focus:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QColor(theme.CONTROL_FOCUS))
                painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 9, 9)
        finally:
            painter.end()


class _ToolScroll(SmoothScrollArea):
    interacted = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.motion.wheel_started.connect(lambda _direction: self.interacted.emit())

    def wheelEvent(self, event) -> None:
        self.interacted.emit()
        super().wheelEvent(event)
        event.accept()


class _ToolItem(QWidget):
    interacted = Signal()
    layout_changed = Signal()

    def __init__(self, view: ToolView, state: ToolItemState, parent=None) -> None:
        super().__init__(parent)
        self._state = state
        self._view: ToolView | None = None
        self.presentation = None
        self._details: ToolDetails | None = None
        self._disclosure: AnimatedDisclosure | None = None
        self._document: QTextDocument | None = None
        self._ranges: tuple[tuple[int, int], ...] = ()
        self._height_limit = theme.TOOL_OUTPUT_MAX_HEIGHT
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.header = _Header()
        layout.addWidget(self.header)
        self.header.toggled.connect(self._set_expanded)
        self.update_view(view)
        self.header.setChecked(state.expanded)

    def update_view(self, view: ToolView) -> None:
        if self._view == view:
            return
        previous = self.presentation
        self._view = view
        self.presentation = present_tool(view)
        display = self.presentation
        self.header.set_summary(display.title, display.icon, view.status == "running", display.annotation)
        if previous is None or previous.body != display.body:
            self._ranges = ()
            if self._document is not None:
                self._document.setPlainText(display.body)
        if self._details is not None:
            self._details.set_presentation(display)
            self._details.set_highlights(self._ranges)
            self._disclosure.refresh_height()

    def _ensure_details(self) -> ToolDetails:
        if self._details is None:
            self._details = ToolDetails(self.presentation, self._state, self)
            self._details.set_height_limit(self._height_limit)
            self._details.set_highlights(self._ranges)
            self._details.interacted.connect(self.interacted.emit)
            self._disclosure = AnimatedDisclosure(self._details)
            self._details.layout_changed.connect(self._disclosure.refresh_height)
            self._disclosure.geometry_changed.connect(self.layout_changed.emit)
            self.layout().addWidget(self._disclosure)
        return self._details

    def _set_expanded(self, expanded: bool) -> None:
        self.interacted.emit()
        self._state.expanded = expanded
        if expanded:
            self._ensure_details()
        if self._disclosure is not None:
            self._disclosure.set_expanded(expanded)
        self.layout_changed.emit()

    def set_height_limit(self, height: int) -> None:
        self._height_limit = height
        if self._details is not None:
            self._details.set_height_limit(height)

    def save_state(self) -> None:
        self._state.expanded = self.header.isChecked()
        if self._details is not None:
            self._details.save_state()

    def search_document(self) -> QTextDocument:
        # 折叠内容也可搜索；此时只创建纯文本索引，不创建Shell控件。
        if self._document is None:
            self._document = QTextDocument(self)
            self._document.setPlainText(self.presentation.body)
        return self._document

    def set_highlights(self, ranges: tuple[tuple[int, int], ...]) -> None:
        self._ranges = ranges
        if self._details is not None:
            self._details.set_highlights(ranges)

    def reveal(self, start: int, end: int):
        self.header.setChecked(True)
        details = self._ensure_details()
        self._disclosure.finish_animation()
        return details.reveal(start, end)


class ToolGroup(QWidget):
    interacted = Signal()
    layout_changed = Signal()

    def __init__(self, views: tuple[ToolView, ...], state: ToolGroupState, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("toolGroup")
        self._state = state
        expanded = state.expanded
        self._items: dict[str, _ToolItem] = {}
        self._views: tuple[ToolView, ...] | None = None
        self._order: tuple[str, ...] = ()
        self._generation = 0
        self._pending_scroll: int | None = state.scroll
        self._restore_generation = 0
        self._restore_timer = QTimer(self)
        self._restore_timer.setSingleShot(True)
        self._restore_timer.timeout.connect(self._restore_scroll)
        self._list_limit = theme.TOOL_LIST_MAX_HEIGHT
        self._output_limit = theme.TOOL_OUTPUT_MAX_HEIGHT
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.header = _Header()
        self._list = _ToolScroll()
        self._list.setObjectName("toolList")
        self._list.setWidgetResizable(True)
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._content = QWidget()
        self._content.setObjectName("toolListContent")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 2, 3, 2)
        self._content_layout.setSpacing(4)
        self._list.setWidget(self._content)
        layout.addWidget(self.header)
        self._card = QFrame()
        self._card.setObjectName("toolListCard")
        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.addWidget(self._list)
        self._disclosure = AnimatedDisclosure(self._card)
        self._disclosure.geometry_changed.connect(self.layout_changed.emit)
        self._disclosure.settled.connect(self._restore_scroll)
        layout.addWidget(self._disclosure)
        self.header.toggled.connect(self._set_expanded)
        self._list.interacted.connect(self._interact)
        self._list.verticalScrollBar().sliderPressed.connect(self._interact)
        self._list.verticalScrollBar().actionTriggered.connect(self._interact)
        self.set_tools(views)
        self.header.setChecked(expanded)

    def _interact(self, *_args) -> None:
        self._generation += 1
        self._restore_timer.stop()
        self._pending_scroll = None
        self.interacted.emit()

    def save_state(self) -> ToolGroupState:
        self._state.expanded = self.header.isChecked()
        self._state.scroll = self._pending_scroll if self._pending_scroll is not None else self._list.verticalScrollBar().value()
        for item in self._items.values():
            item.save_state()
        return self._state

    def set_tools(self, views: tuple[ToolView, ...]) -> None:
        if views == self._views:
            return
        self._views = views
        saved = self.save_state().scroll
        wanted = {view.call_id for view in views}
        for call_id in tuple(self._items):
            if call_id not in wanted:
                item = self._items.pop(call_id)
                self._content_layout.removeWidget(item)
                item.deleteLater()
        for index, view in enumerate(views):
            item = self._items.get(view.call_id)
            if item is None:
                state = self._state.items.setdefault(view.call_id, ToolItemState())
                item = _ToolItem(view, state, self._content)
                item.set_height_limit(self._output_limit)
                item.interacted.connect(self._interact)
                item.layout_changed.connect(self._fit_list)
                self._items[view.call_id] = item
            else:
                item.update_view(view)
        order = tuple(view.call_id for view in views)
        if order != self._order:
            for index, call_id in enumerate(order):
                self._content_layout.insertWidget(index, self._items[call_id])
            self._order = order
        displays = tuple(self._items[call_id].presentation for call_id in self._order)
        self.header.set_summary(*group_summary(views, displays))
        self._fit_list()
        self._queue_restore(saved)

    def _set_expanded(self, expanded: bool) -> None:
        saved = self.save_state().scroll
        self._interact()
        self._state.expanded = expanded
        self._fit_list()
        self._disclosure.set_expanded(expanded)
        self._queue_restore(saved)
        self.layout_changed.emit()

    def _fit_list(self) -> None:
        self._content_layout.activate()
        width = max(1, self._list.viewport().width())
        natural = (self._content_layout.heightForWidth(width) if self._content_layout.hasHeightForWidth()
                   else self._content_layout.sizeHint().height())
        natural = max(1, natural)
        if self._content.height() != natural:
            self._content.setFixedHeight(natural)
        height = min(self._list_limit, natural + self._list.frameWidth() * 2)
        if self._list.height() != height:
            self._list.setFixedHeight(height)
            self._card.setFixedHeight(height + 12)
            self._disclosure.refresh_height()
            self.layout_changed.emit()

    def _queue_restore(self, value: int) -> None:
        self._pending_scroll = value
        if not self.header.isChecked():
            return
        self._restore_generation = self._generation
        self._restore_timer.start(0)

    def _restore_scroll(self) -> None:
        if self.isVisible() and self._restore_generation == self._generation and self._pending_scroll is not None:
            self._list.verticalScrollBar().setValue(self._pending_scroll)
            self._pending_scroll = None

    def documents(self):
        return tuple((call_id, self._items[call_id].search_document()) for call_id in self._order)

    def set_highlights(self, call_id: str, ranges: tuple[tuple[int, int], ...]) -> None:
        self._items[call_id].set_highlights(ranges)

    def clear_highlights(self) -> None:
        for item in self._items.values():
            item.set_highlights(())

    def reveal(self, call_id: str, start: int, end: int):
        self.header.setChecked(True)
        self._disclosure.finish_animation()
        self._interact()
        editor, cursor = self._items[call_id].reveal(start, end)
        self._fit_list()
        point = editor.viewport().mapTo(self._content, editor.cursorRect(cursor).center())
        self._list.ensureVisible(point.x(), point.y(), 12, 20)
        return editor, cursor

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        ancestor = self.parentWidget()
        while ancestor is not None and ancestor.objectName() != "transcriptScroll":
            ancestor = ancestor.parentWidget()
        available = ancestor.viewport().height() if ancestor is not None else theme.TOOL_LIST_MAX_HEIGHT + 24
        self._list_limit = min(theme.TOOL_LIST_MAX_HEIGHT, max(140, available - 24))
        self._output_limit = min(theme.TOOL_OUTPUT_MAX_HEIGHT, max(70, self._list_limit - 90))
        for item in self._items.values():
            item.set_height_limit(self._output_limit)
        self._fit_list()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._pending_scroll is not None:
            self._queue_restore(self._pending_scroll)

    def hideEvent(self, event) -> None:
        saved = self.save_state().scroll
        self._restore_timer.stop()
        self._pending_scroll = saved
        super().hideEvent(event)
