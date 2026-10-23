"""上翻时的回底圆钮；动画只表示当前会话真实任务仍在运行。"""
import math

from PySide6.QtCore import QEasingCurve, QElapsedTimer, Property, QPropertyAnimation, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QAbstractButton

from ..common.icons import icon
from ..common.surfaces import paint_card
from ..shell import theme


class TailButton(QAbstractButton):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(48, 58)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("回到对话底部")
        self.setToolTip("回到最新消息并恢复跟随")
        self._working, self._hover, self._shown = False, False, False
        self._keyboard_focus = False
        self._reveal = 0.0
        self._clock = QElapsedTimer()
        self._clock.start()
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self._fade = QPropertyAnimation(self, b"reveal", self)
        self._fade.setDuration(theme.FEEDBACK_APPEAR_DURATION_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self._finish_fade)
        self.hide()

    @Property(float)
    def reveal(self) -> float:
        return self._reveal

    @reveal.setter
    def reveal(self, value: float) -> None:
        self._reveal = value
        self.update()

    def set_away(self, away: bool) -> None:
        if away == self._shown:
            return
        self._shown = away
        self._fade.stop()
        if away:
            self.show()
            self.raise_()
        self.setEnabled(away)
        self._fade.setStartValue(self._reveal)
        self._fade.setEndValue(1.0 if away else 0.0)
        self._fade.start()
        self._sync_timer()

    def _finish_fade(self) -> None:
        if not self._shown:
            self.hide()

    def set_working(self, working: bool) -> None:
        if self._working != working:
            self._working = working
            self.update()
            self._sync_timer()

    def _sync_timer(self) -> None:
        if self._working and self._shown and not self._hover and not self._keyboard_focus and self.isVisible():
            self._timer.start()
        else:
            self._timer.stop()

    def _tick(self) -> None:
        if self.visibleRegion().isEmpty():
            self._timer.stop()
        else:
            self.update()

    def enterEvent(self, event) -> None:
        self._hover = True
        self._sync_timer()
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self._sync_timer()
        self.update()
        super().leaveEvent(event)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_timer()

    def hideEvent(self, event) -> None:
        self._timer.stop()
        self._fade.stop()
        self._reveal = 1.0 if self._shown else 0.0
        super().hideEvent(event)

    def focusInEvent(self, event) -> None:
        self._keyboard_focus = event.reason() in {Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason}
        self._sync_timer()
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self._keyboard_focus = False
        super().focusOutEvent(event)
        self._sync_timer()
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setOpacity(self._reveal)
            painter.translate(0, 8 * (1 - self._reveal))
            paint_card(painter, QRectF(6, 6, 36, 36), radius=18,
                       background=theme.HOVER if self._hover or self.isDown() else theme.BACKGROUND)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            if self._working and not self._hover and not self._keyboard_focus:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(theme.MUTED))
                phase = self._clock.elapsed() / 1000 * math.tau
                for index in range(3):
                    bounce = max(0.0, math.sin(phase - index * 0.65)) * 4
                    painter.drawEllipse(QRectF(13 + index * 8, 23 - bounce, 4, 4))
            else:
                icon("arrow-down", theme.TEXT, 18).paint(painter, QRect(15, 15, 18, 18))
            if self._keyboard_focus:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QColor(theme.MUTED))
                painter.drawEllipse(QRectF(7, 7, 34, 34))
        finally:
            painter.end()
