"""一轮工作过程的折叠视图；子消息仍复用正文和工具组件。"""
from copy import deepcopy
from dataclasses import dataclass, field
from time import perf_counter
from collections import OrderedDict

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QToolButton, QVBoxLayout, QWidget

from ...attachments.store import ImageStore
from ..common.disclosure import AnimatedDisclosure
from ..common.icons import icon
from ..common.motion import HoverMotion
from ..common.status_text import StatusText
from ..shell import theme
from .message import MessageBlock, MessageViewState
from .projection import DisplayRow
from .tools.models import ToolGroupState
from .viewport_rows import RowSeat, pinned_widget


@dataclass
class WorkProcessState:
    # None表示跟随默认收展；手动选择后不再由完成事件覆盖。
    expanded: bool | None = None
    messages: dict[str, MessageViewState] = field(default_factory=dict)
    tools: dict[str, ToolGroupState] = field(default_factory=dict)


class _ProcessHeader(QToolButton):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._motion = HoverMotion(self)
        self.setFixedHeight(30)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(6)
        self.label = StatusText()
        self.arrow = QLabel()
        self.arrow.setFixedSize(12, 12)
        self.arrow.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.label)
        layout.addWidget(self.arrow)
        self.toggled.connect(self._arrow)

    def _arrow(self, expanded: bool) -> None:
        self.arrow.setPixmap(icon("chevron" if expanded else "right", theme.MUTED, 12).pixmap(QSize(12, 12)))

    def set_status(self, title: str, running: bool, available: bool) -> None:
        self.label.set_status(title, running)
        self.arrow.setVisible(available)
        self._arrow(self.isChecked())
        self.setEnabled(available)
        self.setToolTip("展开工作过程" if available and not self.isChecked() else "收起工作过程" if available else title)
        self.setAccessibleName(title)
        # 先应用继承字体，再用实际布局求总宽度；时长标签不参与压缩省略。
        self.label.ensurePolished()
        self.label.setFixedWidth(self.label.sizeHint().width() + 2)
        self.setFixedWidth(self.layout().sizeHint().width())

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            color = QColor(theme.PRESSED if self.isDown() else theme.HOVER)
            color.setAlphaF(1.0 if self.isDown() else self._motion.progress)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(self.rect()), 7, 7)
            if self._motion.keyboard_focus:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QColor(theme.CONTROL_FOCUS))
                painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 7, 7)
        finally:
            painter.end()


class WorkProcessBlock(QWidget):
    page_requested = Signal(str, str)
    interacted = Signal()
    layout_changed = Signal()

    def __init__(self, row: DisplayRow, images: ImageStore, parent=None, *, state: WorkProcessState | None = None) -> None:
        super().__init__(parent)
        self._images = images
        self._state = state or WorkProcessState()
        self._blocks: dict[str, MessageBlock] = {}
        self._highlights = {}
        self._seats = {}
        self._recent = OrderedDict()
        self._area = None
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_contents)
        self._viewport_height = 400
        self._showing = False
        self.row = None
        self._height_timer = QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.timeout.connect(self._settle_height)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.header = _ProcessHeader()
        layout.addWidget(self.header, 0, Qt.AlignmentFlag.AlignLeft)
        separator = QFrame()
        separator.setFixedHeight(1)
        separator.setStyleSheet(f"background: {theme.BORDER}; border: none;")
        layout.addWidget(separator)
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 12, 0, 4)
        # 中间文字没有复制操作空行，段落和工具之间保留轻量间距。
        self._content_layout.setSpacing(8)
        self._disclosure = AnimatedDisclosure(self._content)
        self._disclosure.geometry_changed.connect(self.layout_changed.emit)
        layout.addWidget(self._disclosure)
        self.header.toggled.connect(self._toggle)
        self.update_row(row)

    def update_row(self, row: DisplayRow) -> bool:
        if self.row == row:
            return False
        first = self.row is None
        previous = self.row
        contents_changed = first or previous.process.rows != row.process.rows
        self.row = row
        expanded = self._state.expanded if self._state.expanded is not None else row.process.running
        if expanded and (contents_changed or not self._showing):
            self._sync_contents()
        elif contents_changed:
            self._prune_contents()
        showing = expanded and bool(row.process.rows)
        changed_expanded = self._showing != showing
        self._showing = showing
        self.header.blockSignals(True)
        self.header.setChecked(expanded)
        self.header.blockSignals(False)
        self.header.set_status(row.text, row.process.running, bool(row.process.rows))
        if first or changed_expanded:
            self._height_timer.stop()
            self._disclosure.set_expanded(showing, animate=not first)
        elif contents_changed:
            self._schedule_height()
        # 每秒只变时长标题时，不重复同步子文档、工具JSON、搜索索引和滚动布局。
        return first or contents_changed or changed_expanded

    def _remember(self, key: str, block: MessageBlock) -> None:
        self._state.messages[key] = deepcopy(block.save_view_state())
        tools = block.save_tool_state()
        if tools is not None:
            self._state.tools[key] = deepcopy(tools)

    def _prune_contents(self) -> None:
        wanted = {row.key for row in self.row.process.rows}
        for key in tuple(self._seats):
            if key not in wanted:
                block = self._blocks.pop(key, None)
                self._recent.pop(key, None)
                if block is not None:
                    self._remember(key, block)
                seat = self._seats.pop(key)
                self._content_layout.removeWidget(seat)
                seat.hide()
                seat.deleteLater()

    def _sync_contents(self) -> None:
        rows = self.row.process.rows
        self._prune_contents()
        changed = False
        for index, row in enumerate(rows):
            seat = self._seats.get(row.key)
            if seat is None:
                seat = self._seats[row.key] = RowSeat(row, max(200, self.width()), self._content)
                self._content_layout.insertWidget(index, seat)
                changed = True
            else:
                changed = seat.update_row(row, max(200, self.width())) or changed
                if self._content_layout.indexOf(seat) != index:
                    self._content_layout.insertWidget(index, seat)
                    changed = True
            block = self._blocks.get(row.key)
            if block is not None and block.row != row:
                changed = block.update_row(row) or changed
        if changed:
            self._schedule_height()
        self._schedule_contents()

    def _ensure_child(self, key):
        if key not in self._blocks:
            row = self._seats[key].row
            block = MessageBlock(row, self._images, self._seats[key],
                                 tool_state=deepcopy(self._state.tools.get(key)),
                                 view_state=deepcopy(self._state.messages.get(key)))
            block.interacted.connect(self._interact)
            block.layout_changed.connect(self._content_changed)
            block.page_requested.connect(self.page_requested.emit)
            block.set_viewport_height(self._viewport_height)
            self._blocks[key] = block
            self._recent[key] = None
            self._seats[key].mount(block)
            for call_id, ranges in self._highlights.get(key, {}).items():
                block.set_highlights(call_id, ranges)
        return self._blocks[key]

    def _schedule_contents(self, *_args):
        if self.isVisible() and self.header.isChecked() and not self._render_timer.isActive():
            self._render_timer.start(16)

    def _render_contents(self):
        if self._area is None or not self.isVisible() or not self.header.isChecked():
            return
        viewport = self._area.viewport()
        self._content_layout.activate()
        height = viewport.height()
        wanted = []
        for key, seat in self._seats.items():
            y = seat.mapTo(viewport, seat.rect().topLeft()).y()
            if y < height * 3 and y + seat.height() > -height * 2:
                wanted.append((key, y))
        deadline, changed = perf_counter() + .006, False
        for key, _y in sorted(wanted, key=lambda item: abs(item[1])):
            if key not in self._blocks:
                self._ensure_child(key)
                changed = True
                if perf_counter() >= deadline:
                    break
            self._recent.move_to_end(key)
        wanted_keys = {key for key, _y in wanted}
        for key in tuple(self._recent):
            if len(self._blocks) <= max(20, len(wanted_keys)):
                break
            if key not in wanted_keys and not pinned_widget(self._blocks[key]):
                block = self._blocks.pop(key)
                self._remember(key, block)
                self._recent.pop(key)
                self._seats[key].unmount()
                block.hide()
                block.deleteLater()
        if changed:
            self._schedule_height()
        if any(key not in self._blocks for key, _y in wanted):
            self._schedule_contents()

    def _content_changed(self) -> None:
        self._schedule_height()
        self._schedule_contents()

    def _schedule_height(self) -> None:
        # 一次消息更新常有多个子高度通知；回到事件循环后只测量一次父抽屉。
        if not self._height_timer.isActive():
            self._height_timer.start(0)

    def _settle_height(self) -> None:
        self._disclosure.refresh_height()
        self.layout_changed.emit()

    def _toggle(self, expanded: bool) -> None:
        self._state.expanded = expanded
        self.interacted.emit()
        if expanded:
            self._sync_contents()
        self.header.setToolTip("收起工作过程" if expanded else "展开工作过程")
        self._showing = expanded and bool(self.row.process.rows)
        self._height_timer.stop()
        self._disclosure.set_expanded(self._showing)

    def _interact(self) -> None:
        # 已进入过程阅读时保留展开，完成自动收起不能抢走正在查看的内容。
        self._state.expanded = self.header.isChecked()
        self.interacted.emit()

    def retain_expanded(self) -> None:
        if self.header.isChecked():
            self._state.expanded = True

    def save_state(self) -> WorkProcessState:
        for key, block in self._blocks.items():
            self._remember(key, block)
        return self._state

    def child_message_state(self, key: str) -> MessageViewState | None:
        return self._state.messages.get(key)

    def set_viewport_height(self, height: int) -> None:
        self._viewport_height = height
        for block in self._blocks.values():
            block.set_viewport_height(height)
        for seat in self._seats.values():
            seat.reflow(max(200, self.width()))
        self._schedule_contents()

    def reading_part(self, viewport):
        if not self.header.isChecked():
            return None
        for row in self.row.process.rows:
            key = row.key
            seat = self._seats.get(key)
            if seat is None:
                continue
            y = seat.mapTo(viewport, seat.rect().topLeft()).y()
            if y + seat.height() > 0:
                child = self._blocks.get(key)
                part = child.reading_part(viewport) if child is not None else None
                return ((key, part[0]), part[1]) if part else (key, y)
        return None

    def part_widget(self, key):
        if isinstance(key, tuple):
            child, part = key
            block = self._blocks.get(child)
            return block.part_widget(part) if block is not None else None
        return self._seats.get(key)

    def showEvent(self, event):
        super().showEvent(event)
        if self._area is None:
            parent = self.parentWidget()
            while parent is not None:
                if isinstance(parent, QScrollArea) and parent.objectName() == "transcriptScroll":
                    self._area = parent
                    parent.verticalScrollBar().valueChanged.connect(self._schedule_contents)
                    parent.verticalScrollBar().rangeChanged.connect(self._schedule_contents)
                    break
                parent = parent.parentWidget()
        self._schedule_contents()

    def hideEvent(self, event):
        self._render_timer.stop()
        self._height_timer.stop()
        super().hideEvent(event)

    def documents(self):
        # 保留旧控件接口；正文搜索使用独立临时文档，不走此处。
        self._sync_contents()
        return tuple(((row.key, call_id), document) for row in self.row.process.rows
                     for call_id, document in self._ensure_child(row.key).documents())

    def set_highlights(self, target, ranges) -> None:
        key, call_id = target
        self._highlights.setdefault(key, {})[call_id] = ranges
        if key in self._blocks:
            self._blocks[key].set_highlights(call_id, ranges)

    def clear_highlights(self) -> None:
        self._highlights.clear()
        for block in self._blocks.values():
            block.clear_highlights()

    def reveal_match(self, target, start: int, end: int):
        key, call_id = target
        self.header.setChecked(True)
        self._sync_contents()
        self._disclosure.finish_animation()
        block = self._ensure_child(key)
        self._content_layout.activate()
        self._disclosure.refresh_height()
        self._disclosure.finish_animation()
        return block.reveal_match(call_id, start, end)
