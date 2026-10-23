"""复用显示行，分别更新持久历史与当前轮次；滚动只属于页面。"""
from copy import deepcopy
from dataclasses import dataclass, replace
from bisect import bisect_right
from collections import OrderedDict
from time import perf_counter
from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from ...attachments.store import ImageStore
from .message import MessageBlock, MessageViewState
from .work_process import WorkProcessBlock, WorkProcessState
from .projection import DisplayRow, TurnMarker
from .tools.models import ToolGroupState
from ..common.widgets import IconGlyph
from ..common.scrolling import SmoothScrollArea
from ..shell import theme
from .viewport_rows import RowSeat, pinned_widget, estimated_height
from .row_geometry import RowGeometry


SearchTarget = str | tuple[str, str | None] | None


class _ScrollArea(SmoothScrollArea):
    user_scrolled = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.motion.wheel_started.connect(self.user_scrolled.emit)

    def keyPressEvent(self, event) -> None:
        self.motion.stop()
        if event.key() in {Qt.Key.Key_Up, Qt.Key.Key_PageUp, Qt.Key.Key_Home}:
            self.user_scrolled.emit(1)
        elif event.key() in {Qt.Key.Key_Down, Qt.Key.Key_PageDown, Qt.Key.Key_End}:
            self.user_scrolled.emit(-1)
        super().keyPressEvent(event)


@dataclass
class _ReadingState:
    follow: bool = True
    position: int = 0
    anchor: tuple[str, int] | None = None
    part: tuple[str | tuple[str, str], int] | None = None


class Transcript(QWidget):
    changed = Signal()
    active_turn_changed = Signal(str)
    away_from_bottom_changed = Signal(bool)
    reading_width_changed = Signal(int)
    action_requested = Signal(str, object, object)
    page_requested = Signal(str, str)

    def __init__(self, images: ImageStore, parent=None) -> None:
        super().__init__(parent)
        self._images = images
        self._blocks: dict[str, MessageBlock | WorkProcessBlock] = {}
        self._highlights = {}
        self._seats: dict[str, RowSeat] = {}
        self._recent = OrderedDict()
        self._geometry = RowGeometry()
        self._viewport_timer = QTimer(self)
        self._viewport_timer.setSingleShot(True)
        self._viewport_timer.timeout.connect(self._render_window)
        self._history: tuple[DisplayRow, ...] = ()
        self._live: tuple[DisplayRow, ...] = ()
        self._history_by_key: dict[str, DisplayRow] = {}
        self._live_by_key: dict[str, DisplayRow] = {}
        self._rows: dict[str, DisplayRow] = {}
        self._latest_copy: str | None = None
        self._order: tuple[str, ...] = ()
        self._markers: tuple[TurnMarker, ...] = ()
        self._marker_indices = ()
        self._marker_keys = ()
        self._conversation_id: str | None = None
        self._positions: dict[str, _ReadingState] = {}
        self._tool_states: dict[str | None, dict[str, ToolGroupState]] = {}
        self._message_states: dict[str | None, dict[str, MessageViewState]] = {}
        self._process_states: dict[str | None, dict[str, WorkProcessState]] = {}
        self._view_order = OrderedDict()
        self._follow = True
        self._anchor: tuple[str, int] | None = None
        self._part_anchor: tuple[str | tuple[str, str], int] | None = None
        self._binding = self._syncing = self._applying_scroll = self._user_pending = False
        self._allow_resume: bool | None = None
        self._away = False
        self._glide_target: str | None = None
        self._scroll_generation = 0
        self._restore: int | None = None
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._settle_scroll)
        self._user_timer = QTimer(self)
        self._user_timer.setSingleShot(True)
        self._user_timer.timeout.connect(self._finish_user_scroll)
        self._reveal_target = None
        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.timeout.connect(self._finish_reveal)
        self._navigation_timer = QTimer(self)
        self._navigation_timer.setSingleShot(True)
        self._navigation_timer.setInterval(40)
        self._navigation_timer.timeout.connect(self._track_turn)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._scroll = _ScrollArea()
        self._scroll.setObjectName("transcriptScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        content = QWidget()
        content.setObjectName("transcriptContent")
        row = QHBoxLayout(content)
        row.setContentsMargins(0, 28, 0, 28)
        row.addStretch(1)
        self._column = QWidget()
        self._column.setObjectName("transcriptColumn")
        self._column.setMaximumWidth(theme.CONTENT_WIDTH)
        self._layout = QVBoxLayout(self._column)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._empty = QWidget()
        welcome = QVBoxLayout(self._empty)
        welcome.setContentsMargins(20, 35, 20, 65)
        welcome.setSpacing(16)
        welcome.addWidget(IconGlyph("chat", size=42, color="#bac3ca"), 0, Qt.AlignmentFlag.AlignCenter)
        self._welcome_title = QLabel("今天想做些什么？")
        self._welcome_title.setObjectName("welcomeTitle")
        self._welcome_title.setTextFormat(Qt.TextFormat.PlainText)
        self._welcome_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._welcome_title.setWordWrap(True)
        self._welcome_caption = QLabel("新建聊天，开始一段对话。")
        self._welcome_caption.setObjectName("welcomeCaption")
        self._welcome_caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        welcome.addWidget(self._welcome_title)
        welcome.addWidget(self._welcome_caption)
        self._layout.addStretch()
        self._layout.addWidget(self._empty)
        self._layout.addStretch()
        row.addWidget(self._column, 100)
        row.addStretch(1)
        self._scroll.setWidget(content)
        self._scroll.viewport().installEventFilter(self)
        content.installEventFilter(self)
        layout.addWidget(self._scroll)
        bar = self._scroll.verticalScrollBar()
        bar.valueChanged.connect(self._value_changed)
        bar.rangeChanged.connect(lambda _min, _max: self._schedule_scroll())
        bar.sliderPressed.connect(lambda: self._user_scroll())
        bar.sliderReleased.connect(self._finish_user_scroll)
        bar.actionTriggered.connect(lambda _action: self._user_scroll())
        self._scroll.user_scrolled.connect(self._user_scroll)
        self._scroll.motion.settled.connect(self._motion_finished)
        self._scroll.motion.cancelled.connect(self._inspect_tool)
        self._update_column_width()

    def set_welcome(self, project_name: str | None) -> None:
        self._welcome_title.setText(f"在 {project_name} 中做些什么？" if project_name else "今天想做些什么？")
        self._welcome_caption.setText("选择一个任务，从这里开始。" if project_name else "输入消息，开始一段对话。")

    @property
    def away_from_bottom(self) -> bool:
        return self._away

    @property
    def reading_width(self) -> int:
        return self._column.width()

    def _update_column_width(self) -> None:
        width = theme.reading_column_width(self._scroll.viewport().width())
        if self._column.width() != width or self._column.minimumWidth() != width:
            self._column.setFixedWidth(width)
            self.reading_width_changed.emit(width)

    def bind_id(self, conversation_id: str | None) -> None:
        if conversation_id == self._conversation_id:
            return
        self._scroll.motion.stop()
        if self._user_pending:
            self._finish_user_scroll()
        if self._conversation_id is not None:
            self._positions[self._conversation_id] = _ReadingState(
                self._follow, self._scroll.verticalScrollBar().value(), self._anchor, self._part_anchor)
        self._binding = True
        self._glide_target = None
        self._scroll_timer.stop()
        self._viewport_timer.stop()
        self._user_timer.stop()
        self._user_pending = False
        self._reveal_timer.stop()
        self._reveal_target = None
        self._scroll_generation += 1
        self._restore = None
        self._history, self._live, self._markers = (), (), ()
        self._highlights.clear()
        # 清空时仍归属旧会话，工具阅读状态不能被记到刚切入的会话。
        self._sync(apply_scroll=False)
        self._conversation_id = conversation_id
        self._view_order[conversation_id] = None
        self._view_order.move_to_end(conversation_id)
        while len(self._view_order) > 32:
            expired, _ = self._view_order.popitem(last=False)
            for states in (self._positions, self._tool_states, self._message_states, self._process_states):
                states.pop(expired, None)
        reading = self._positions.get(conversation_id, _ReadingState())
        self._follow, self._anchor = reading.follow, reading.anchor
        self._part_anchor = reading.part
        self._restore = reading.position if conversation_id in self._positions else None
        self._binding = False
        self._update_tail()

    def set_history(self, rows: tuple[DisplayRow, ...]) -> None:
        if rows == self._history:
            return
        self._history = rows
        self._sync()

    def set_live(self, rows: tuple[DisplayRow, ...]) -> None:
        if rows == self._live:
            return
        same_keys = tuple(row.key for row in rows) == tuple(self._live_by_key)
        self._live = rows
        self._sync(live_only=same_keys)

    def set_markers(self, markers: tuple[TurnMarker, ...]) -> None:
        self._markers = markers
        self._index_markers()

    def _index_markers(self):
        indexed = sorted((self._geometry.positions[item.key], item.key) for item in self._markers
                         if item.key in self._geometry.positions)
        self._marker_indices = tuple(index for index, _ in indexed)
        self._marker_keys = tuple(key for _, key in indexed)

    def refresh_rows(self, decorator):
        """插件状态只更新显示行；不改控制器、日志或模型历史。"""
        history, live = decorator(self._history), decorator(self._live)
        if history != self._history or live != self._live:
            self._history, self._live = history, live
            self._sync()

    def documents(self):
        # 搜索按数据逐项建临时文档，不挂载整段历史界面。
        from .search_documents import search_documents
        return search_documents(tuple(self._rows.values()), self)

    def set_highlights(self, key: str, call_id: SearchTarget, ranges: tuple[tuple[int, int], ...]) -> None:
        self._highlights.setdefault(key, {})[call_id] = ranges
        if key in self._blocks:
            self._blocks[key].set_highlights(call_id, ranges)

    def clear_highlights(self) -> None:
        self._highlights.clear()
        for block in self._blocks.values():
            block.clear_highlights()

    def finish_edits(self) -> None:
        for block in self._blocks.values():
            if isinstance(block, MessageBlock) and block._editor is not None:
                block._cancel_edit()

    def _remember_tools(self, key: str, block: MessageBlock | WorkProcessBlock) -> None:
        if isinstance(block, WorkProcessBlock):
            self._process_states.setdefault(self._conversation_id, {})[key] = deepcopy(block.save_state())
            return
        state = block.save_tool_state()
        if state is not None:
            # 只复制少量阅读位置，避免旧控件的延迟释放改变新控件正在使用的状态。
            self._tool_states.setdefault(self._conversation_id, {})[key] = deepcopy(state)
        self._message_states.setdefault(self._conversation_id, {})[key] = deepcopy(block.save_view_state())

    def _sync(self, *, apply_scroll: bool = True, live_only: bool = False) -> None:
        self._syncing = True
        changed = False
        try:
            previous_copy = self._latest_copy
            previous_rows = self._rows if not live_only else None
            if live_only:
                # 文字增量只比较本轮行，历史索引和排列不重新建立。
                dirty: set[str] = set()
                copy_changed = False
                for row in self._live:
                    if self._live_by_key[row.key] == row:
                        continue
                    self._live_by_key[row.key] = row
                    effective = (row if row.process is not None else self._history_by_key.get(row.key, row))
                    previous = self._rows[row.key]
                    if previous != effective:
                        self._rows[row.key] = effective
                        dirty.add(row.key)
                        copy_changed = copy_changed or previous.copyable != effective.copyable
                if copy_changed:
                    self._latest_copy = next((key for key in reversed(self._rows) if self._rows[key].copyable), None)
                if previous_copy != self._latest_copy:
                    dirty.update(key for key in (previous_copy, self._latest_copy) if key in self._rows)
                keys, pending = self._order, dirty
            else:
                self._history_by_key = {row.key: row for row in self._history}
                self._live_by_key = {row.key: row for row in self._live}
                self._rows = dict(self._history_by_key)
                for row in self._live:
                    if row.process is not None:
                        # 同一过程的实时内容覆盖旧检查点；普通历史行仍然优先。
                        self._rows[row.key] = row
                    else:
                        self._rows.setdefault(row.key, row)
                self._latest_copy = next((key for key in reversed(self._rows) if self._rows[key].copyable), None)
                keys, pending = tuple(self._rows), self._rows
                for key in self._order:
                    if key not in self._rows:
                        self._unmount(key)
                        changed = True
                self._geometry.reset(self._rows, self.reading_width)
            for key in pending:
                row = self._rows[key]
                if row.copyable and key != self._latest_copy:
                    row = replace(row, copyable=False)
                seat = self._seats.get(key)
                if seat is None:
                    if live_only or previous_rows.get(key) != self._rows[key]:
                        self._geometry.update(key, estimated_height(row, self.reading_width))
                        changed = True
                else:
                    seat_changed = seat.update_row(row, self.reading_width)
                    block = self._blocks.get(key)
                    if block is not None and block.row != row:
                        changed = block.update_row(row) or changed
                    elif block is None:
                        changed = seat_changed or changed
            if keys != self._order:
                self._order = keys
                self._index_markers()
                changed = True
            if not live_only:
                self._empty.setVisible(not keys)
        finally:
            self._syncing = False
        if changed:
            self._layout_window()
            self._schedule_window()
            self._reveal_timer.stop()
            self._reveal_target = None
            if apply_scroll:
                self._schedule_scroll()
            self.changed.emit()

    def _ensure_block(self, key):
        if key in self._blocks:
            return self._blocks[key]
        row = self._rows[key]
        self._seats[key] = RowSeat(row, self.reading_width, self._column)
        if row.copyable and key != self._latest_copy:
            row = replace(row, copyable=False)
        state = self._tool_states.get(self._conversation_id, {}).get(key)
        message_state = self._message_states.get(self._conversation_id, {}).get(key)
        if row.process is not None:
            saved = self._process_states.get(self._conversation_id, {}).get(key)
            block = WorkProcessBlock(row, self._images, self._seats[key], state=deepcopy(saved))
        else:
            if message_state is None:
                message_state = next((block.child_message_state(key) for block in self._blocks.values()
                                      if isinstance(block, WorkProcessBlock) and block.child_message_state(key) is not None), None)
            if message_state is None:
                message_state = next((saved.messages[key] for saved in self._process_states.get(self._conversation_id, {}).values()
                                      if key in saved.messages), None)
            block = MessageBlock(row, self._images, self._seats[key], tool_state=deepcopy(state), view_state=deepcopy(message_state))
            block.action_requested.connect(self.action_requested.emit)
        block.interacted.connect(self._inspect_tool)
        block.page_requested.connect(self.page_requested.emit)
        block.layout_changed.connect(self._geometry_changed)
        block.set_viewport_height(self._scroll.viewport().height())
        self._blocks[key] = block
        self._seats[key].mount(block)
        self._seats[key].show()
        self._schedule_window()
        for target, ranges in self._highlights.get(key, {}).items():
            block.set_highlights(target, ranges)
        self._recent[key] = None
        return block

    def _unmount(self, key):
        block = self._blocks.pop(key, None)
        if block is not None:
            self._remember_tools(key, block)
            seat = self._seats.pop(key)
            if key in self._geometry.positions:
                self._geometry.update(key, seat.height())
            self._layout.removeWidget(seat)
            seat.hide()
            seat.deleteLater()
            block.hide()
        self._recent.pop(key, None)

    def _layout_window(self):
        """只为挂载行安排布局，离屏区间用少量固定高度间隔表示。"""
        while self._layout.count():
            self._layout.takeAt(0)
        if not self._order:
            self._layout.addWidget(self._empty)
            return
        position = 0
        for key in sorted(self._seats, key=self._geometry.positions.__getitem__):
            start = self._geometry.offset(key)
            if start > position:
                self._layout.addSpacing(start - position)
            seat = self._seats[key]
            self._layout.addWidget(seat)
            position = start + self._geometry.heights[self._geometry.positions[key]]
        if self._geometry.total > position:
            self._layout.addSpacing(self._geometry.total - position)
        self._layout.activate()

    def _measure_rows(self):
        changed = False
        for key, seat in self._seats.items():
            changed = self._geometry.update(key, seat.height()) or changed
        if changed:
            self._layout_window()
            self._schedule_scroll()
        return changed

    def _schedule_window(self):
        if self.isVisible() and not self._viewport_timer.isActive():
            self._viewport_timer.start(16)

    def _render_window(self):
        if not self.isVisible() or not self._order:
            return
        self._layout.activate()
        self._measure_rows()
        top = -self._column.mapTo(self._scroll.viewport(), self._column.rect().topLeft()).y()
        height = self._scroll.viewport().height()
        start = max(0, self._geometry.locate(top - height * 2) - 1)
        end = min(len(self._order), self._geometry.locate(top + height * 3) + 2)
        wanted = set(self._order[start:end])
        if self._follow:
            wanted.update(self._order[-2:])
        priority = sorted(wanted, key=lambda key: abs(self._geometry.offset(key) - top))
        if self._follow:
            priority = list(reversed(self._order[-2:])) + [key for key in priority if key not in self._order[-2:]]
        anchor = self._order[self._geometry.locate(top)]
        before = self._geometry.offset(anchor)
        deadline, changed = perf_counter() + .006, False
        for key in priority:
            if key not in self._blocks:
                self._ensure_block(key)
                changed = True
                if perf_counter() >= deadline:
                    break
            self._recent.move_to_end(key)
        for key in tuple(self._recent):
            if len(self._blocks) <= max(20, len(wanted)):
                break
            if key not in wanted and not pinned_widget(self._blocks[key]):
                self._unmount(key)
                changed = True
        if changed:
            self._layout_window()
            self._layout.activate()
            self._measure_rows()
            self._scroll.widget().layout().activate()
            if not self._follow and anchor:
                self._applying_scroll = True
                try:
                    self._scroll.motion.shift_vertical(self._geometry.offset(anchor) - before)
                finally:
                    self._applying_scroll = False
            self._schedule_scroll()
        if changed or any(key not in self._blocks for key in wanted):
            self._schedule_window()

    def _user_scroll(self, direction: int | None = None) -> None:
        if direction is None:
            self._scroll.motion.stop()
        self._glide_target = None
        self._follow = False
        if not self._user_pending:
            self._allow_resume = None
        self._user_pending = True
        # Qt 的滚轮处理还会触发滚动条 actionTriggered，不能丢掉原始上翻方向。
        if direction is not None:
            self._allow_resume = direction < 0
        self._scroll_timer.stop()
        self._scroll_generation += 1
        self._restore = None
        self._user_timer.start(0)

    def _finish_user_scroll(self) -> None:
        bar = self._scroll.verticalScrollBar()
        if bar.isSliderDown():
            return
        if self._scroll.motion.active:
            return
        self._user_pending = False
        moving_down = self._allow_resume is not False
        if moving_down and bar.maximum() - bar.value() <= 2:
            self._follow = True
            self._anchor = None
        else:
            self._capture_anchor()
        self._update_tail()

    def _inspect_tool(self) -> None:
        # 选择文字、展开消息和阅读内层输出，均显式暂停跟随。
        self._follow = False
        self._scroll.motion.stop()
        self._glide_target = None
        self._user_pending = False
        self._user_timer.stop()
        self._scroll_timer.stop()
        self._scroll_generation += 1
        self._restore = None
        self._capture_anchor()
        self._update_tail()

    def scroll_to_bottom(self) -> None:
        self._scroll_generation += 1
        self._reveal_timer.stop()
        self._reveal_target = None
        self._user_timer.stop()
        self._user_pending = False
        self._follow, self._anchor, self._restore = True, None, None
        self._scroll_timer.stop()
        self._glide_target = "tail"
        bar = self._scroll.verticalScrollBar()
        duration = min(450, max(180, round((bar.maximum() - bar.value()) * 0.25)))
        self._scroll.motion.move_to(bar.maximum(), duration=duration)
        self._update_tail()

    def resume_follow(self) -> None:
        """发送后的程序跟随直接定位；用户回底按钮才使用滑动动画。"""
        self._scroll.motion.stop()
        self._glide_target = None
        self._follow, self._anchor, self._restore = True, None, None
        self._user_pending = False
        self._schedule_scroll()

    def _motion_finished(self) -> None:
        target, self._glide_target = self._glide_target, None
        if target == "tail":
            self.resume_follow()
        elif target == "marker":
            self._capture_anchor()
            self._schedule_scroll()
        elif self._user_pending:
            self._finish_user_scroll()

    def _capture_anchor(self) -> None:
        if self._order:
            origin = self._column.mapTo(self._scroll.viewport(), self._column.rect().topLeft()).y()
            key = self._order[self._geometry.locate(-origin)]
            y = origin + self._geometry.offset(key)
            body = self._blocks.get(key)
            if isinstance(body, WorkProcessBlock) and not self._follow and y < -body.header.height():
                body.retain_expanded()
            self._anchor = (key, y)
            self._part_anchor = body.reading_part(self._scroll.viewport()) if body is not None else None
            return
        self._anchor = None
        self._part_anchor = None

    def _schedule_scroll(self) -> None:
        if not self._binding and not self._syncing and not self._user_pending and self._glide_target is None and self.isVisible():
            self._scroll_timer.start(0)

    def _settle_scroll(self) -> None:
        if self._binding or self._user_pending or self._glide_target is not None:
            return
        if not self._order:
            self._update_tail()
            return
        self._layout.activate()
        self._scroll.widget().layout().activate()
        bar = self._scroll.verticalScrollBar()
        self._applying_scroll = True
        try:
            if self._follow:
                bar.setValue(bar.maximum())
            elif self._anchor is not None and self._anchor[0] in self._geometry.positions:
                key, offset = self._anchor
                block = self._seats.get(key)
                body = self._blocks.get(key)
                y = self._column.mapTo(self._scroll.viewport(), self._column.rect().topLeft()).y() + self._geometry.offset(key)
                if self._part_anchor is not None and body is not None:
                    part, part_offset = self._part_anchor
                    widget = body.part_widget(part)
                    if widget is not None:
                        block, offset = widget, part_offset
                        y = block.mapTo(self._scroll.viewport(), block.rect().topLeft()).y()
                self._scroll.motion.shift_vertical(y - offset)
            elif self._restore is not None:
                bar.setValue(self._restore)
            self._restore = None
        finally:
            self._applying_scroll = False
        if not self._follow:
            self._capture_anchor()
        self._update_tail()
        self._track_turn()
        self._schedule_window()

    def _value_changed(self, _value: int) -> None:
        self._schedule_window()
        if not self._navigation_timer.isActive():
            self._navigation_timer.start()
        self._update_tail()

    def _update_tail(self) -> None:
        bar = self._scroll.verticalScrollBar()
        away = bool(self._order) and (not self._follow or self._glide_target == "tail") and bar.maximum() - bar.value() > 2
        if away != self._away:
            self._away = away
            self.away_from_bottom_changed.emit(away)

    def _geometry_changed(self) -> None:
        self._schedule_window()
        if not self._syncing:
            self._schedule_scroll()

    def eventFilter(self, watched, event) -> bool:
        if watched is self._scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._update_column_width()
            # 输入框变高只改变视口高度，不必重建全部历史的宽度估算。
            if self._geometry.width != self.reading_width:
                self._geometry.reset(self._rows, self.reading_width)
                for seat in self._seats.values():
                    seat.reflow(self.reading_width)
                self._layout_window()
            for block in self._blocks.values():
                block.set_viewport_height(self._scroll.viewport().height())
            self._schedule_scroll()
        elif watched is self._scroll.widget() and event.type() == QEvent.Type.LayoutRequest:
            self._schedule_scroll()
        return super().eventFilter(watched, event)

    def jump(self, key: str) -> None:
        if key in self._geometry.positions:
            self._inspect_tool()
            origin = self._column.mapTo(self._scroll.widget(), self._column.rect().topLeft()).y()
            self._glide_target = "marker"
            self._scroll.motion.move_to(max(0, origin + self._geometry.offset(key) - 20), duration=180)
            self._update_tail()
            self.active_turn_changed.emit(key)

    def reveal(self, key: str, call_id: SearchTarget, start: int, end: int) -> None:
        if key not in self._rows:
            return
        block = self._ensure_block(key)
        self._layout_window()
        self._inspect_tool()
        block.reveal_match(call_id, start, end)
        block.set_highlights(call_id, self._highlights.get(key, {}).get(call_id, ()))
        self._reveal_target = (self._scroll_generation, key, call_id, start, end)
        self._reveal_timer.start(0)

    def _finish_reveal(self) -> None:
        target, self._reveal_target = self._reveal_target, None
        if target is None or target[0] != self._scroll_generation:
            return
        _, key, call_id, start, end = target
        block = self._blocks.get(key)
        if block is None:
            return
        editor, cursor = block.reveal_match(call_id, start, end)
        self._layout.activate()
        point = editor.viewport().mapTo(self._scroll.widget(), editor.cursorRect(cursor).center())
        self._scroll.ensureVisible(point.x(), point.y(), 20, 65)
        self._capture_anchor()
        self._update_tail()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._schedule_scroll()

    def hideEvent(self, event) -> None:
        self._viewport_timer.stop()
        self._navigation_timer.stop()
        self._scroll.motion.stop()
        if self._user_pending:
            self._finish_user_scroll()
        self._glide_target = None
        self._user_pending = False
        self._scroll_timer.stop()
        self._user_timer.stop()
        self._reveal_timer.stop()
        super().hideEvent(event)

    def _track_turn(self, _value: int | None = None) -> None:
        if not self._marker_indices or not self._order:
            return
        origin = self._column.mapTo(self._scroll.viewport(), self._column.rect().topLeft()).y()
        target = self._geometry.locate(70 - origin)
        position = max(0, bisect_right(self._marker_indices, target) - 1)
        self.active_turn_changed.emit(self._marker_keys[position])
