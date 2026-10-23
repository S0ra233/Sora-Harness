"""工具与上下文状态共用文字扫光；隐藏或结束时不保留刷新定时器。"""
from PySide6.QtCore import QElapsedTimer, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..shell import theme


class StatusSpinner(QWidget):
    """工作状态的小圆环；退出运行或离开页面时停止绘制计时器。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self._active = False
        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self.update)

    def set_active(self, active: bool) -> None:
        self._active = active
        if active and self.isVisible():
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.set_active(self._active)

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            ring = QRectF(2, 2, 12, 12)
            painter.setPen(QPen(QColor(theme.BORDER), 1.5))
            painter.drawEllipse(ring)
            if self._active:
                painter.setPen(QPen(QColor(theme.MUTED), 1.5))
                painter.drawArc(ring, -round(self._clock.elapsed() % 1000 / 1000 * 360 * 16), 255 * 16)
        finally:
            painter.end()


class StatusText(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        font = self.font()
        font.setFamilies(theme.TEXT_FAMILIES)
        font.setPixelSize(theme.TOOL_FONT_SIZE)
        self.setFont(font)
        self._text, self._active = "", False
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(theme.TOOL_ANIMATION_FRAME_MS)
        self._timer.timeout.connect(self._tick)

    def set_status(self, text: str, active: bool) -> None:
        if (text, active) == (self._text, self._active):
            return
        self._text = text
        if active and not self._active:
            self._clock.start()
        self._active = active
        if not active:
            self._timer.stop()
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(self.fontMetrics().horizontalAdvance(self._text), self.fontMetrics().height() + 8)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.fontMetrics().height() + 8)

    def _tick(self) -> None:
        if self._active and not self.visibleRegion().isEmpty():
            self.update()
        else:
            self._timer.stop()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            rect = self.contentsRect()
            text = self.fontMetrics().elidedText(self._text, Qt.TextElideMode.ElideRight, rect.width())
            flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            painter.setPen(QColor(theme.TOOL_TEXT))
            painter.drawText(rect, flags, text)
            if self._active:
                if not self._timer.isActive():
                    self._timer.start()
                width = max(1, self.fontMetrics().horizontalAdvance(text))
                beam = min(85, max(24, width // 3))
                fraction = (self._clock.elapsed() % theme.TOOL_SWEEP_DURATION_MS) / theme.TOOL_SWEEP_DURATION_MS
                center = fraction * (width + beam * 2) - beam
                gradient = QLinearGradient(center - beam, 0, center + beam, 0)
                gradient.setColorAt(0, QColor(255, 255, 255, 0))
                gradient.setColorAt(0.5, QColor(255, 255, 255, 80))
                gradient.setColorAt(1, QColor(255, 255, 255, 0))
                painter.setPen(QPen(QBrush(gradient), 0))
                painter.drawText(rect, flags, text)
        finally:
            painter.end()

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)
