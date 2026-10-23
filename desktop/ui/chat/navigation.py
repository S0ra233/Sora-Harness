"""用户轮次刻度；指针附近横线按高斯分布变长，滚轮由父滚动区处理。"""
import math

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget

from ..shell import theme
from .projection import TurnMarker


class _TurnPreview(QFrame):
    """浮在滚动区外的预览，使用纯文本，不能被刻度视口裁切。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_WindowPropagation)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setObjectName("turnPreview")
        self.setFixedWidth(350)
        self.setStyleSheet(f"QFrame#turnPreview {{ background: white; border: 1px solid {theme.BORDER}; border-radius: 14px; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        self._user = QLabel()
        self._reply = QLabel()
        for label, lines, color in ((self._user, 3, theme.TEXT), (self._reply, 2, theme.MUTED)):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            size, weight = (12, 600) if label is self._user else (11, 400)
            label.setStyleSheet(f"color: {color}; font-size: {size}px; font-weight: {weight}; background: transparent; border: none;")
            label.setMaximumHeight(label.fontMetrics().lineSpacing() * lines + 4)
            layout.addWidget(label)

    def show_marker(self, marker: TurnMarker, point: QPoint) -> None:
        self._user.setText(marker.user)
        self._reply.setText(marker.reply)
        self._reply.setVisible(bool(marker.reply))
        self.adjustSize()
        area = self.parentWidget().screen().availableGeometry()
        self.move(max(area.left() + 8, min(point.x(), area.right() - self.width() - 8)),
                  max(area.top() + 8, min(point.y(), area.bottom() - self.height() - 8)))
        self.show()


class _Marks(QWidget):
    activated = Signal(str)
    focused = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("turnMarks")
        self.markers: tuple[TurnMarker, ...] = ()
        self.active: str | None = None
        self._target: float | None = None
        self._wave = 0.0
        self._gain = 0.0
        self._focus = 0
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._animate)
        self._preview = _TurnPreview(self)

    def set_markers(self, markers: tuple[TurnMarker, ...]) -> None:
        if tuple(marker.key for marker in markers) != tuple(marker.key for marker in self.markers):
            self._preview.hide()
        self.markers = markers
        self._focus = min(self._focus, max(0, len(markers) - 1))
        self.setMinimumHeight(len(markers) * 14 + 32)
        if self._preview.isVisible():
            self._preview.show_marker(markers[self._focus], self.mapToGlobal(QPoint(38, round(self._y(self._focus)) - 20)))
        self.update()

    def _y(self, index: int) -> float:
        return (self.height() - max(0, len(self.markers) - 1) * 14) / 2 + index * 14

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for index, marker in enumerate(self.markers):
            y = self._y(index)
            if y < event.rect().top() - 2 or y > event.rect().bottom() + 2:
                continue
            length = (14 if marker.key == self.active else 7) + 21 * self._gain * math.exp(-((y - self._wave) ** 2) / (2 * 40 ** 2))
            painter.setPen(QPen(QColor(theme.TEXT if marker.key == self.active else theme.BORDER), 2))
            painter.drawLine(QPoint(2, round(y)), QPoint(round(2 + length), round(y)))

    def _nearest(self, y: float) -> int | None:
        if not self.markers:
            return None
        return min(max(round((y - self._y(0)) / 14), 0), len(self.markers) - 1)

    def mouseMoveEvent(self, event) -> None:
        self._target = event.position().y()
        if not self._timer.isActive():
            self._timer.start()
        index = self._nearest(self._target)
        if index is not None and abs(self._target - self._y(index)) <= 12:
            self._focus = index
            marker = self.markers[index]
            self._preview.show_marker(marker, self.mapToGlobal(QPoint(38, round(self._y(index)) - 20)))
        else:
            self._preview.hide()

    def leaveEvent(self, event) -> None:
        self._target = None
        self._preview.hide()
        self._timer.start()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        index = self._nearest(event.position().y())
        if event.button() == Qt.MouseButton.LeftButton and index is not None and abs(event.position().y() - self._y(index)) <= 12:
            self.activated.emit(self.markers[index].key)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in {Qt.Key.Key_Up, Qt.Key.Key_Down} and self.markers:
            direction = -1 if event.key() == Qt.Key.Key_Up else 1
            self._focus = min(max(self._focus + direction, 0), len(self.markers) - 1)
            self._target = self._y(self._focus)
            self._timer.start()
            self.focused.emit(round(self._target))
            event.accept()
        elif event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter} and self.markers:
            self.activated.emit(self.markers[self._focus].key)
            event.accept()
        else:
            super().keyPressEvent(event)

    def _animate(self) -> None:
        goal = 1.0 if self._target is not None else 0.0
        self._gain += (goal - self._gain) * 0.3
        if self._target is not None:
            self._wave += (self._target - self._wave) * 0.3
        self.update()
        if abs(goal - self._gain) < 0.01 and (self._target is None or abs(self._wave - self._target) < 0.1):
            self._gain = goal
            self._timer.stop()

    def hideEvent(self, event) -> None:
        self._timer.stop()
        self._preview.hide()
        super().hideEvent(event)


class TurnNavigation(QScrollArea):
    activated = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("turnNavigation")
        self.setFixedWidth(32)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._marks = _Marks()
        self.setWidget(self._marks)
        self._marks.activated.connect(self.activated.emit)
        self._marks.focused.connect(lambda y: self.ensureVisible(15, y, 0, 20))
        self.hide()

    def set_markers(self, markers: tuple[TurnMarker, ...]) -> None:
        if self._marks.markers != markers:
            self._marks.set_markers(markers)
        self.setVisible(bool(markers))

    def set_active(self, key: str) -> None:
        if self._marks.active != key:
            self._marks.active = key
            self._marks.update()
