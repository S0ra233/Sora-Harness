"""参考截图的圆点滑条；只处理绘制与实际档位位置，不推断模型能力。"""
from PySide6.QtCore import QEasingCurve, QPointF, Property, QPropertyAnimation, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSlider, QStyle

from ..shell import theme
from ..common.motion import HoverMotion


class EffortSlider(QSlider):
    def __init__(self, parent=None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setFixedHeight(36)
        self.setSingleStep(1)
        self.setPageStep(1)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("推理强度")
        self._motion = HoverMotion(self)
        self._visual = float(self.value())
        self._snap = QPropertyAnimation(self, b"visualPosition", self)
        self._snap.setDuration(theme.CONTROL_SNAP_DURATION_MS)
        self._snap.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.valueChanged.connect(self._value_changed)
        self.rangeChanged.connect(lambda _low, _high: self._set_visual(float(self.value())))

    @Property(float)
    def visualPosition(self) -> float:
        return self._visual

    @visualPosition.setter
    def visualPosition(self, value: float) -> None:
        self._visual = value
        self.update()

    def _set_visual(self, value: float) -> None:
        self._snap.stop()
        self.visualPosition = value

    def setValue(self, value: int) -> None:
        super().setValue(value)
        if self.signalsBlocked():
            self._set_visual(float(self.value()))

    def setRange(self, minimum: int, maximum: int) -> None:
        super().setRange(minimum, maximum)
        self._set_visual(float(self.value()))

    def _value_changed(self, value: int) -> None:
        if self.isSliderDown() or not self.isVisible():
            self._set_visual(float(value))
        else:
            self._animate_snap()

    def _animate_snap(self) -> None:
        self._snap.stop()
        self._snap.setStartValue(self._visual)
        self._snap.setEndValue(float(self.value()))
        self._snap.start()

    def _drag_visual(self, x: float) -> None:
        fraction = min(1.0, max(0.0, (x - 16) / max(1, self.width() - 32)))
        if self.invertedAppearance():
            fraction = 1.0 - fraction
        self._set_visual(self.minimum() + fraction * (self.maximum() - self.minimum()))

    def _point_value(self, x: float) -> int:
        span = max(1, self.width() - 32)
        position = min(span, max(0, round(x - 16)))
        return QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), position, span, self.invertedAppearance())

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.setSliderDown(True)
            self.setSliderPosition(self._point_value(event.position().x()))
            self._drag_visual(event.position().x())
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self.isSliderDown():
            self.setSliderPosition(self._point_value(event.position().x()))
            self._drag_visual(event.position().x())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self.setSliderPosition(self._point_value(event.position().x()))
            self.setSliderDown(False)
            self._animate_snap()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def wheelEvent(self, event) -> None:
        event.ignore()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            center = self.height() / 2
            track = QRectF(2, center - 13, max(0, self.width() - 4), 26)
            count = self.maximum() - self.minimum()
            fraction = (self._visual - self.minimum()) / count if count else 0
            if self.invertedAppearance():
                fraction = 1 - fraction
            x = 16 + max(0, self.width() - 32) * fraction
            path = QPainterPath()
            path.addRoundedRect(track, 13, 13)
            painter.fillPath(path, QColor(theme.EFFORT_TRACK))
            painter.save()
            painter.setClipPath(path)
            painter.fillRect(QRectF(track.left(), track.top(), x - track.left(), track.height()), QColor(theme.EFFORT_FILL))
            painter.restore()
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(theme.EFFORT_BORDER), 1))
            painter.drawPath(path)
            painter.setPen(Qt.PenStyle.NoPen)
            for value in range(self.minimum(), self.maximum() + 1):
                if value == self.value():
                    continue
                position = (value - self.minimum()) / count if count else 0
                if self.invertedAppearance():
                    position = 1 - position
                dot_x = 16 + max(0, self.width() - 32) * position
                painter.setBrush(QColor(theme.EFFORT_PASSED_DOT if dot_x < x else theme.EFFORT_DOT))
                painter.drawEllipse(QPointF(dot_x, center), 2.5, 2.5)
            painter.setBrush(QColor(38, 45, 54, 12))
            painter.drawEllipse(QPointF(x, center + 1), 16, 16)
            painter.setBrush(QColor(theme.BACKGROUND))
            painter.setPen(QPen(QColor(theme.CONTROL_FOCUS if self._motion.keyboard_focus else theme.EFFORT_BORDER), 1))
            painter.drawEllipse(QPointF(x, center), 15, 15)
        finally:
            painter.end()

    def hideEvent(self, event) -> None:
        self._set_visual(float(self.value()))
        super().hideEvent(event)
