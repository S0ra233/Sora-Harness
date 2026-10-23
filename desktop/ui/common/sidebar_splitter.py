"""两种侧栏共用拖动外壳；窗口收窄不改用户保存的宽度。"""
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QSplitter, QSplitterHandle

from ...application.ui_preferences import UiPreferencesStore
from ..shell import theme


class _Handle(QSplitterHandle):
    def __init__(self, orientation, parent) -> None:
        super().__init__(orientation, parent)
        self._hover = False

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            center = self.width() // 2
            painter.fillRect(0, 0, center, self.height(), QColor(theme.SIDEBAR))
            painter.fillRect(center, 0, self.width() - center, self.height(), QColor(theme.BACKGROUND))
            # 设置页用两种底色直接分阶；拖动区域保留，不再加截图中的淡竖线。
            if self.splitter()._name != "settings":
                painter.setPen(QPen(QColor(theme.CONTROL_BORDER if self._hover else theme.BORDER), 1))
                painter.drawLine(center, 0, center, self.height())
        finally:
            painter.end()


class SidebarSplitter(QSplitter):
    preferences_failed = Signal(str)

    def __init__(self, sidebar, body, *, name: str,
                 preferences: UiPreferencesStore | None = None, parent=None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._sidebar, self._preferences, self._name = sidebar, preferences, name
        self._preferred = preferences.width(name) if preferences else theme.SIDEBAR_WIDTH
        self._updating, self._transition, self._expanded = False, False, True
        self.setHandleWidth(6)
        sidebar.setMinimumWidth(0)
        sidebar.setMaximumWidth(450)
        sidebar.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        body.setMinimumWidth(320)
        self.addWidget(sidebar)
        self.addWidget(body)
        self.setCollapsible(0, True)
        self.setCollapsible(1, False)
        self.setStretchFactor(0, 0)
        self.setStretchFactor(1, 1)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self.flush_preferences)
        self.splitterMoved.connect(self._dragged)
        self._apply_preference()

    def createHandle(self):
        return _Handle(self.orientation(), self)

    def _available(self) -> int:
        return max(0, self.width() - self.handleWidth() - self.widget(1).minimumWidth())

    @property
    def preferred_width(self) -> int:
        return self._preferred

    @property
    def extent(self) -> int:
        return self.sizes()[0]

    def set_extent(self, value: int) -> None:
        value = min(max(0, value), self._available())
        self._updating = True
        try:
            self.setSizes([value, max(0, self.width() - self.handleWidth() - value)])
        finally:
            self._updating = False

    def begin_transition(self) -> None:
        self._transition = True
        self._sidebar.show()

    def end_transition(self, expanded: bool) -> None:
        self._transition, self._expanded = False, expanded
        self._sidebar.setVisible(expanded)
        self._apply_preference()

    def _apply_preference(self) -> None:
        if self._expanded and not self._transition:
            self.set_extent(self._preferred)

    def _dragged(self, position: int, index: int) -> None:
        if self._updating or self._transition or not self._expanded:
            return
        width = min(450, max(180, position))
        self._preferred = width
        self.set_extent(width)
        if self._preferences is not None:
            self._preferences.set_width(self._name, width)
            self._save_timer.start()

    def flush_preferences(self) -> None:
        self._save_timer.stop()
        if self._preferences is not None:
            try:
                self._preferences.save()
            except (OSError, ValueError) as exc:
                self.preferences_failed.emit(f"侧栏宽度尚未保存：{exc}")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_preference()

    def hideEvent(self, event) -> None:
        self.flush_preferences()
        super().hideEvent(event)
