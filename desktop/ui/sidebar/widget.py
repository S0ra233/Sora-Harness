"""侧栏接收目录快照，发出带稳定 ID 的用户意图。"""
from PySide6.QtCore import QEvent, QEasingCurve, QModelIndex, Property, QParallelAnimationGroup, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QKeySequence, QLinearGradient, QPainter, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QMenu, QStackedWidget,
    QProxyStyle, QStyle, QTreeView, QVBoxLayout, QWidget,
)

from ...application.catalog import CatalogSnapshot
from ..common.icons import icon
from ..common.widgets import IconButton
from ..common.surfaces import FadeVeil
from ..common.scrolling import ScrollMotion
from ..common.buttons import MotionButton
from ..shell import theme
from .delegate import SidebarDelegate
from .model import ID_ROLE, KIND_ROLE, PIN_ROLE, SidebarModel
from .fold import SidebarFold


class _SidebarStyle(QProxyStyle):
    """行的背景与箭头归自定义委托，禁止Windows风格再绘制选中边条。"""

    def drawPrimitive(self, element, option, painter, widget=None) -> None:
        if element in {QStyle.PrimitiveElement.PE_PanelItemViewRow, QStyle.PrimitiveElement.PE_FrameFocusRect,
                       QStyle.PrimitiveElement.PE_IndicatorBranch}:
            return
        super().drawPrimitive(element, option, painter, widget)

    def styleHint(self, hint, option=None, widget=None, returnData=None) -> int:
        if hint == QStyle.StyleHint.SH_ToolTip_WakeUpDelay:
            return 120
        return super().styleHint(hint, option, widget, returnData)


class _SidebarTree(QTreeView):
    """行内动作消费自己的点击，不能同时触发打开会话的 clicked。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pressed_action: tuple[str, str] | None = None
        # 不接管共享的应用Style；此代理由该视图持有，保留键盘选择和焦点。
        style = _SidebarStyle()
        style.setParent(self)
        self.setStyle(style)
        self.fold = SidebarFold(self)

    def update_hover(self) -> None:
        index = self.indexAt(self.viewport().mapFromGlobal(QCursor.pos())) if self.viewport().underMouse() else QModelIndex()
        self.itemDelegate().set_hover(index)

    def mouseMoveEvent(self, event) -> None:
        self.itemDelegate().set_hover(self.indexAt(event.position().toPoint()))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self.itemDelegate().set_hover(QModelIndex())
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.verticalScrollBar().value() < self.verticalScrollBar().maximum():
            painter = QPainter(self.viewport())
            try:
                height = min(24, self.viewport().height())
                top = self.viewport().height() - height
                gradient = QLinearGradient(0, top, 0, self.viewport().height())
                clear = QColor(theme.SIDEBAR)
                clear.setAlpha(0)
                gradient.setColorAt(0, clear)
                gradient.setColorAt(1, QColor(theme.SIDEBAR))
                painter.fillRect(0, top, self.viewport().width(), height, gradient)
            finally:
                painter.end()

    def keyPressEvent(self, event) -> None:
        index = self.currentIndex()
        if index.isValid() and index.data(KIND_ROLE) in {"project", "section"}:
            key = event.key()
            open_key = key in {Qt.Key.Key_Right, Qt.Key.Key_Plus}
            close_key = key in {Qt.Key.Key_Left, Qt.Key.Key_Minus}
            if (open_key and not self.isExpanded(index)) or (close_key and self.isExpanded(index)):
                self.fold.toggle(index)
                event.accept()
                return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:
        self._pressed_action = None
        index = self.indexAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and index.isValid():
            action = self.itemDelegate().action_at(index, self.visualRect(index), event.position().toPoint())
            if action is not None:
                self._pressed_action = (action, index.data(ID_ROLE))
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        pressed, self._pressed_action = self._pressed_action, None
        index = self.indexAt(event.position().toPoint())
        if pressed is not None:
            if event.button() == Qt.MouseButton.LeftButton and index.isValid():
                delegate = self.itemDelegate()
                action = delegate.action_at(index, self.visualRect(index), event.position().toPoint())
                if pressed == (action, index.data(ID_ROLE)):
                    delegate.action_requested.emit(action, index.data(ID_ROLE))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        index = self.indexAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and index.isValid() and index.data(KIND_ROLE) in {"project", "section"}:
            action = self.itemDelegate().action_at(index, self.visualRect(index), event.position().toPoint())
            if action is None:
                # Qt默认把第二次释放标为双击，不再发clicked；收展标题仍按一次普通按下处理。
                self.mousePressEvent(event)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)


class ConversationSidebar(QWidget):
    conversation_requested = Signal(str)
    new_requested = Signal(object)
    project_requested = Signal()
    rename_requested = Signal(str)
    pin_requested = Signal(str)
    archive_requested = Signal(str)
    collapse_requested = Signal()
    refresh_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("conversationSidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setMinimumWidth(0)
        self.setMaximumWidth(450)
        self._snapshot = CatalogSnapshot()
        self.model = SidebarModel(self)
        self._delegate = SidebarDelegate(self)
        self._collapsed: set[str] = set()
        self._history_expanded = False
        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._animate)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 16, 10, 12)
        layout.setSpacing(12)
        self._header = QStackedWidget()
        normal = QWidget()
        heading = QHBoxLayout(normal)
        heading.setContentsMargins(13, 0, 0, 0)
        heading.setSpacing(4)
        collapse = IconButton("sidebar", "收起会话侧栏")
        search = IconButton("search", "搜索会话标题")
        title = QLabel("工作台")
        title.setObjectName("sidebarTitle")
        heading.addWidget(title, 1)
        heading.addWidget(collapse)
        heading.addWidget(search)
        search_row = QWidget()
        search_layout = QHBoxLayout(search_row)
        self._search_layout = search_layout
        search_layout.setContentsMargins(0, 0, 0, 0)
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索会话标题")
        self._search.setClearButtonEnabled(True)
        close_search = IconButton("close", "关闭标题搜索")
        search_layout.addWidget(self._search, 1)
        search_layout.addWidget(close_search)
        self._header.addWidget(normal)
        self._header.addWidget(search_row)
        self._search_open = False
        self._search_row = search_row
        self._search_veil = FadeVeil(search_row, color=theme.SIDEBAR)
        search_row.installEventFilter(self)
        self._fade = QPropertyAnimation(self._search_veil, b"opacity", self)
        self._slide = QPropertyAnimation(self, b"searchOffset", self)
        self._search_animation = QParallelAnimationGroup(self)
        for animation in (self._fade, self._slide):
            animation.setDuration(160)
            animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._search_animation.addAnimation(animation)
        self._search_animation.finished.connect(self._finish_search_animation)
        self._new = MotionButton("新建聊天", variant="ghost")
        self._new.setObjectName("newChatButton")
        self._new.setIcon(icon("new"))
        self._new.setFixedHeight(36)
        self._view = _SidebarTree()
        self._view.motion = ScrollMotion(self._view)
        self._view.motion.wheel_started.connect(lambda _direction: self._view.fold.stop())
        self._view.setModel(self.model)
        self._view.setItemDelegate(self._delegate)
        self._delegate.repaint_requested.connect(self._view.viewport().update)
        self._view.verticalScrollBar().valueChanged.connect(lambda _value: self._view.update_hover())
        self._view.setHeaderHidden(True)
        self._view.setRootIsDecorated(False)
        # Qt原生树动画没有公开时长设置；由可见区过渡实际控制80ms，不再只改样式开关。
        self._view.setAnimated(False)
        self._view.setExpandsOnDoubleClick(False)
        self._view.setIndentation(0)
        self._view.setMouseTracking(True)
        self._view.setUniformRowHeights(False)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        footer = QHBoxLayout()
        self._history_more = MotionButton("展开显示", variant="ghost")
        self._history_more.setObjectName("sidebarMore")
        self._history_more.hide()
        self._history_more.clicked.connect(self._toggle_history)
        footer.addWidget(self._history_more)
        footer.addStretch()
        refresh = IconButton("refresh", "刷新会话目录", size=26)
        footer.addWidget(refresh)
        layout.addWidget(self._header)
        layout.addWidget(self._new)
        layout.addWidget(self._view, 1)
        layout.addLayout(footer)
        self._history_veil = FadeVeil(self._view.viewport(), color=theme.SIDEBAR)
        self._history_fade = QPropertyAnimation(self._history_veil, b"opacity", self)
        self._history_fade.setDuration(theme.SIDEBAR_GROUP_DURATION_MS)
        self._history_fade.finished.connect(self._history_veil.hide)
        collapse.clicked.connect(self.collapse_requested.emit)
        self.collapse_button = collapse
        search.clicked.connect(self.open_search)
        close_search.clicked.connect(self._close_search)
        self._search.textChanged.connect(self._filter)
        self._new.clicked.connect(lambda: self.new_requested.emit(None))
        refresh.clicked.connect(self.refresh_requested.emit)
        self._view.clicked.connect(self._open_row)
        self._view.collapsed.connect(lambda index: self._collapsed.add(index.data(ID_ROLE)))
        self._view.expanded.connect(lambda index: self._collapsed.discard(index.data(ID_ROLE)))
        self._view.customContextMenuRequested.connect(self._menu)
        self._delegate.action_requested.connect(self._action)
        escape = QShortcut(QKeySequence("Esc"), self._search)
        escape.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        escape.activated.connect(self._close_search)
        self._view.expandAll()

    def set_snapshot(self, snapshot: CatalogSnapshot) -> None:
        self._snapshot = snapshot
        self._apply_snapshot()

    def _apply_snapshot(self) -> None:
        self._view.fold.stop()
        selected = self.model.active_id
        # 搜索时临时展开命中分组，关闭后恢复用户原来的折叠选择。
        collapsed = set(self._collapsed)
        if self.model.set_snapshot(self._snapshot, self._search.text()):
            self._view.expandAll()
            if not self._search.text().strip():
                for section in range(self.model.rowCount()):
                    index = self.model.index(section, 0)
                    if index.data(ID_ROLE) in collapsed:
                        self._view.collapse(index)
                    for row in range(self.model.rowCount(index)):
                        child = self.model.index(row, 0, index)
                        if child.data(ID_ROLE) in collapsed:
                            self._view.collapse(child)
            self._collapsed = collapsed
            indexes = self.model.conversation_indexes(selected) if selected else ()
            if indexes:
                self._view.setCurrentIndex(indexes[-1])
        self._limit_history()
        self._view.update_hover()

    def _limit_history(self) -> None:
        """只限制列表呈现，完整目录保留；搜索期间显示全部命中，当前会话始终可见。"""
        searching = bool(self._search.text().strip())
        limited = False
        self._view.setUpdatesEnabled(False)
        try:
            for section in range(self.model.rowCount()):
                parent = self.model.index(section, 0)
                limit = 6 if parent.data(ID_ROLE) == "projects" else 12
                for row in range(self.model.rowCount(parent)):
                    index = self.model.index(row, 0, parent)
                    contains_active = index.data(ID_ROLE) == self.model.active_id or any(
                        self.model.index(child, 0, index).data(ID_ROLE) == self.model.active_id
                        for child in range(self.model.rowCount(index)))
                    limited = limited or row >= limit
                    hide = not searching and not self._history_expanded and row >= limit and not contains_active
                    self._view.setRowHidden(row, parent, hide)
                    for child in range(self.model.rowCount(index)):
                        child_index = self.model.index(child, 0, index)
                        limited = limited or child >= 8
                        self._view.setRowHidden(child, index, not searching and not self._history_expanded
                                                and child >= 8 and child_index.data(ID_ROLE) != self.model.active_id)
        finally:
            self._view.setUpdatesEnabled(True)
        self._history_more.setVisible(limited and not searching)
        self._history_more.setText("收起显示" if self._history_expanded else "展开显示")

    def _toggle_history(self) -> None:
        self._view.fold.stop()
        self._history_expanded = not self._history_expanded
        opacity = self._history_veil.opacity if self._history_veil.isVisible() else 1.0
        self._history_fade.stop()
        self._history_veil.setGeometry(self._view.viewport().rect())
        self._history_veil.opacity = opacity
        self._history_veil.show()
        self._history_veil.raise_()
        self._limit_history()
        self._history_fade.setStartValue(opacity)
        self._history_fade.setEndValue(0.0)
        self._history_fade.start()

    def set_active(self, conversation_id: str | None) -> None:
        changed = self.model.active_id != conversation_id
        self.model.set_active(conversation_id)
        if changed:
            self._limit_history()
        if changed and conversation_id is not None:
            indexes = self.model.conversation_indexes(conversation_id)
            if indexes:
                self._view.setCurrentIndex(indexes[-1])

    def set_running(self, conversation_id: str, busy: bool) -> None:
        self.model.set_running(conversation_id, busy)
        self._update_animation()

    def set_new_enabled(self, enabled: bool) -> None:
        self._new.setEnabled(enabled)
        self._new.setToolTip("当前普通聊天还是空白，请先使用它。" if not enabled else "新建普通聊天")

    def open_search(self) -> None:
        was_visible = self._header.currentIndex() == 1
        self._search_open = True
        self._search_animation.stop()
        self._header.setCurrentIndex(1)
        self._search_veil.setGeometry(self._search_row.rect())
        self._search_veil.show()
        self._search_veil.raise_()
        self._fade.setStartValue(self._search_veil.opacity if was_visible else 1.0)
        self._fade.setEndValue(0.0)
        self._slide.setStartValue(self.searchOffset if was_visible else 9)
        self._slide.setEndValue(0)
        self._search_animation.start()
        self._search.setFocus()

    def _close_search(self) -> None:
        self._search_open = False
        self._search_animation.stop()
        self._search.clear()
        self._search_veil.show()
        self._search_veil.raise_()
        self._fade.setStartValue(self._search_veil.opacity)
        self._fade.setEndValue(1.0)
        self._slide.setStartValue(self.searchOffset)
        self._slide.setEndValue(9)
        self._search_animation.start()

    def _finish_search_animation(self) -> None:
        self._search_veil.hide()
        if not self._search_open:
            self._header.setCurrentIndex(0)

    def eventFilter(self, watched, event) -> bool:
        if watched is self._search_row and event.type() == QEvent.Type.Resize:
            self._search_veil.setGeometry(self._search_row.rect())
        return super().eventFilter(watched, event)

    @Property(int)
    def searchOffset(self) -> int:
        return self._search_layout.contentsMargins().left()

    @searchOffset.setter
    def searchOffset(self, value: int) -> None:
        self._search_layout.setContentsMargins(value, 0, 0, 0)

    def _filter(self, _text: str) -> None:
        self._apply_snapshot()

    def _open_row(self, index) -> None:
        if index.data(KIND_ROLE) == "conversation":
            self.conversation_requested.emit(index.data(ID_ROLE))
        else:
            self._view.fold.toggle(index)

    def _action(self, action: str, identifier: str) -> None:
        if action == "more":
            index = QModelIndex(self._delegate.hover_index)
            if index.isValid() and index.data(ID_ROLE) == identifier:
                self._show_menu(index, self._view.viewport().mapToGlobal(self._view.visualRect(index).bottomRight()))
        elif action == "rename":
            self.rename_requested.emit(identifier)
        elif action == "pin":
            self.pin_requested.emit(identifier)
        elif action == "archive":
            self.archive_requested.emit(identifier)
        elif identifier == "projects":
            self.project_requested.emit()
        else:
            self.new_requested.emit(identifier)

    def _menu(self, point) -> None:
        index = self._view.indexAt(point)
        if not index.isValid():
            return
        self._show_menu(index, self._view.viewport().mapToGlobal(point))

    def _show_menu(self, index, point) -> None:
        kind, identifier = index.data(KIND_ROLE), index.data(ID_ROLE)
        menu = QMenu(self)
        if kind == "conversation":
            menu.addAction("编辑名称", lambda: self.rename_requested.emit(identifier))
            menu.addAction("取消置顶" if index.data(PIN_ROLE) else "置顶", lambda: self.pin_requested.emit(identifier))
            menu.addAction("归档", lambda: self.archive_requested.emit(identifier))
        elif kind == "project":
            menu.addAction("新建项目对话", lambda: self.new_requested.emit(identifier))
        elif identifier == "projects":
            menu.addAction("添加项目", self.project_requested.emit)
        else:
            menu.deleteLater()
            return
        menu.exec(point)
        menu.deleteLater()

    def _update_animation(self) -> None:
        if self.model.running and self.isVisible():
            self._timer.start()
        else:
            self._timer.stop()

    def _animate(self) -> None:
        self._delegate.angle = (self._delegate.angle + 24) % 360
        for conversation_id in self.model.running:
            for index in self.model.conversation_indexes(conversation_id):
                rect = self._view.visualRect(index)
                if rect.intersects(self._view.viewport().rect()):
                    self._view.viewport().update(rect)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._update_animation()

    def hideEvent(self, event) -> None:
        self._timer.stop()
        self._history_fade.stop()
        self._history_veil.hide()
        super().hideEvent(event)
