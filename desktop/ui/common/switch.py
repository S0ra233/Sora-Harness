"""已有布尔配置的胶囊开关；值和信号仍由Qt按钮提供。"""
from PySide6.QtCore import QEasingCurve, Property, QPropertyAnimation, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton

from .motion import HoverMotion
from ..shell import theme


class SwitchToggle(QAbstractButton):
    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(44, 24)
        self.setAccessibleName(title)
        self.setToolTip(title)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._position = 0.0
        self._motion = HoverMotion(self)
        self._slide = QPropertyAnimation(self, b"handlePosition", self)
        self._slide.setDuration(theme.SWITCH_DURATION_MS)
        self._slide.setEasingCurve(QEasingCurve.Type.OutQuad)
        self.toggled.connect(self._toggle)

    @Property(float)
    def handlePosition(self) -> float:
        return self._position

    @handlePosition.setter
    def handlePosition(self, value: float) -> None:
        self._position = value
        self.update()

    def _toggle(self, checked: bool) -> None:
        self._slide.stop()
        target = 1.0 if checked else 0.0
        if self.isVisible():
            self._slide.setDuration(max(1, round(theme.SWITCH_DURATION_MS * abs(target - self._position))))
            self._slide.setStartValue(self._position)
            self._slide.setEndValue(target)
            self._slide.start()
        else:
            self.handlePosition = target

    def setChecked(self, checked: bool) -> None:
        super().setChecked(checked)
        if self.signalsBlocked():
            self._slide.stop()
            self.handlePosition = 1.0 if self.isChecked() else 0.0

    def hideEvent(self, event) -> None:
        self._slide.stop()
        self.handlePosition = 1.0 if self.isChecked() else 0.0
        super().hideEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            if not self.isEnabled():
                painter.setOpacity(0.5)
            off, on, progress = QColor(theme.BORDER), QColor("#3b82f6"), self._position
            color = QColor(round(off.red() + (on.red() - off.red()) * progress),
                           round(off.green() + (on.green() - off.green()) * progress),
                           round(off.blue() + (on.blue() - off.blue()) * progress))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(0, 0, 44, 24), 12, 12)
            painter.setBrush(QColor(0, 0, 0, 22))
            painter.drawEllipse(QRectF(2 + progress * 20, 3, 20, 20))
            painter.setBrush(QColor("white"))
            painter.drawEllipse(QRectF(2 + progress * 20, 2, 20, 20))
            if self._motion.keyboard_focus:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(theme.CONTROL_FOCUS), 1))
                painter.drawRoundedRect(QRectF(0.5, 0.5, 43, 23), 12, 12)
        finally:
            painter.end()
