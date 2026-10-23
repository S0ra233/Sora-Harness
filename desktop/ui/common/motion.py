"""按钮的悬停与提示共用状态；只在交互时动画，不用图形效果。"""
from PySide6.QtCore import QEvent, QEasingCurve, QObject, QPoint, QPropertyAnimation, QRectF, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QCursor, QPainter
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

from ..shell import theme


class _Tip(QFrame):
    def __init__(self, owner) -> None:
        super().__init__(owner, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        self.label.setMaximumWidth(330)
        self.label.setStyleSheet("background: transparent; color: white; font-size: 11px;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(11, 8, 11, 8)
        layout.addWidget(self.label)
        self.fade = QPropertyAnimation(self, b"windowOpacity", self)
        self.fade.setDuration(theme.TOOLTIP_DURATION_MS)
        self.fade.setEasingCurve(QEasingCurve.Type.OutQuad)

    def present(self, text: str, point: QPoint) -> None:
        self.label.setText(text)
        self.adjustSize()
        area = self.parentWidget().screen().availableGeometry()
        self.move(max(area.left() + 4, min(point.x(), area.right() - self.width() - 4)),
                  max(area.top() + 4, min(point.y(), area.bottom() - self.height() - 4)))
        if not self.isVisible():
            self.setWindowOpacity(0.0)
            self.show()
            self.fade.setStartValue(0.0)
            self.fade.setEndValue(1.0)
            self.fade.start()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QColor(theme.DARK_CARD_BG))
            painter.setBrush(QColor(theme.DARK_CARD_BG))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        finally:
            painter.end()

    def hideEvent(self, event) -> None:
        self.fade.stop()
        super().hideEvent(event)


class HoverMotion(QObject):
    def __init__(self, owner) -> None:
        super().__init__(owner)
        self.owner, self.progress, self.keyboard_focus = owner, 0.0, False
        self.press_offset = 0.0
        self._pressed = False
        self._tip = None
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(theme.HOVER_DURATION_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.Linear)
        self._animation.valueChanged.connect(self._tick)
        self._press_animation = QVariantAnimation(self)
        self._press_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._press_animation.valueChanged.connect(self._press_tick)
        self._tip_timer = QTimer(self)
        self._tip_timer.setSingleShot(True)
        self._tip_timer.setInterval(theme.TOOLTIP_DELAY_MS)
        self._tip_timer.timeout.connect(self._show_tip)
        owner.installEventFilter(self)

    def _tick(self, value) -> None:
        self.progress = float(value)
        self.owner.update()

    def _hover(self, entered: bool) -> None:
        self._animation.stop()
        self._animation.setStartValue(self.progress)
        self._animation.setEndValue(1.0 if entered else 0.0)
        self._animation.start()

    def _press_tick(self, value) -> None:
        self.press_offset = float(value)
        self.owner.update()

    def _press(self, pressed: bool) -> None:
        if pressed == self._pressed:
            return
        self._pressed = pressed
        self._press_animation.stop()
        self._press_animation.setDuration(theme.BUTTON_PRESS_DURATION_MS if pressed else theme.BUTTON_RELEASE_DURATION_MS)
        self._press_animation.setStartValue(self.press_offset)
        self._press_animation.setEndValue(1.0 if pressed else 0.0)
        self._press_animation.start()

    def _reset_motion(self) -> None:
        self._animation.stop()
        self._press_animation.stop()
        self._pressed = False
        self.progress = self.press_offset = 0.0
        self._hide_tip()
        self.owner.update()

    def _show_tip(self) -> None:
        if not self.owner.isVisible() or not self.owner.toolTip():
            return
        if self._tip is None:
            self._tip = _Tip(self.owner)
        self._tip.present(self.owner.toolTip(), QCursor.pos() + QPoint(12, 18))

    def _hide_tip(self) -> None:
        self._tip_timer.stop()
        if self._tip is not None:
            self._tip.hide()

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Enter:
            self._hover(True)
            self._tip_timer.start()
        elif kind == QEvent.Type.Leave:
            self._hover(False)
            self._hide_tip()
        elif kind == QEvent.Type.FocusIn:
            self.keyboard_focus = event.reason() in {Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason}
            self.owner.update()
        elif kind == QEvent.Type.FocusOut:
            self.keyboard_focus = False
            self._press(False)
            self._hide_tip()
            self.owner.update()
        elif kind == QEvent.Type.MouseButtonPress:
            self._hide_tip()
            if event.button() == Qt.MouseButton.LeftButton and self.owner.isEnabled():
                self._press(True)
        elif kind == QEvent.Type.MouseButtonRelease:
            self._press(False)
            if self.owner.underMouse():
                self._tip_timer.start()
        elif kind in {QEvent.Type.KeyPress, QEvent.Type.KeyRelease} and event.key() in {Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            if not event.isAutoRepeat() and self.owner.isEnabled():
                self._press(kind == QEvent.Type.KeyPress)
        elif kind == QEvent.Type.Hide:
            self._reset_motion()
        elif kind == QEvent.Type.EnabledChange and not self.owner.isEnabled():
            self._reset_motion()
        elif kind == QEvent.Type.ToolTip:
            # 原生迟到的 Tooltip 不再覆盖120ms的统一提示卡。
            return True
        return False
