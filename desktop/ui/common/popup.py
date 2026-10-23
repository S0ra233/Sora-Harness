"""模型与项目共用的白色卡片浮层；不改动 Qt 私有下拉容器。"""
from PySide6.QtCore import QEvent, QEasingCurve, QPropertyAnimation, QRectF, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QApplication, QFrame

from .surfaces import paint_card
from .focus import FocusReturn, cycle_focus
from ..shell import theme


class CardPopup(QFrame):
    closed = Signal()

    def __init__(self, parent, name: str) -> None:
        super().__init__(parent.window(), Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setObjectName(name)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay)
        self.setAutoFillBackground(False)
        self.setStyleSheet(theme.model_popup_stylesheet())
        self._anchor = parent
        self._anchor_toggle = None
        self._anchor_pressed = False
        self._filtering = False
        self._requested = False
        self.radius = theme.POPUP_RADIUS
        self._focus_return = FocusReturn(self)
        self._return_on_hide = False
        self._closing = False
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setEasingCurve(QEasingCurve.Type.OutQuad)
        self._fade.finished.connect(self._finish_fade)

    @property
    def is_open(self) -> bool:
        """点击依目标状态切换，不把关闭动画中的可见卡片当作仍要打开。"""
        return self._requested

    def bind_toggle(self, callback) -> None:
        """弹层取得鼠标时，原按钮的按压仍交回同一个切换入口。"""
        self._anchor_toggle = callback

    def _watch_anchor(self, enabled: bool) -> None:
        app = QApplication.instance()
        if app is not None and enabled != self._filtering:
            (app.installEventFilter if enabled else app.removeEventFilter)(self)
            self._filtering = enabled

    def eventFilter(self, watched, event) -> bool:
        if event.type() == QEvent.Type.ApplicationDeactivate:
            self._anchor_pressed = False
            self._watch_anchor(False)
            self.hide()
            return False
        if event.type() == QEvent.Type.MouseButtonRelease and self._anchor_pressed and event.button() == Qt.MouseButton.LeftButton:
            self._anchor_pressed = False
            if not self.isVisible():
                self._watch_anchor(False)
            return True
        if (event.type() in {QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick} and event.button() == Qt.MouseButton.LeftButton
                and self.isVisible() and self._anchor_toggle is not None and self._anchor.isVisible()
                and self._anchor.rect().contains(self._anchor.mapFromGlobal(event.globalPosition().toPoint()))):
            # Qt会在鼠标事件中清除WA_NoMouseReplay；只在构造时设置不能保证不重开。
            self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay)
            self._anchor_pressed = True
            self._anchor_toggle()
            return True
        return super().eventFilter(watched, event)

    def present(self) -> None:
        if not self.isVisible():
            self._focus_return.capture(self._anchor)
        self._return_on_hide = False
        self._fade.stop()
        self._requested = True
        self._closing = False
        if self._anchor_toggle is not None:
            self._watch_anchor(True)
        if not self.isVisible():
            self.setWindowOpacity(0.0)
            self.show()
        self._fade.setDuration(max(1, round(theme.POPUP_ENTER_DURATION_MS * (1 - self.windowOpacity()))))
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(1.0)
        self._fade.start()

    def dismiss(self, *, restore_focus: bool = False) -> None:
        self._requested = False
        self._return_on_hide = restore_focus
        if not self.isVisible():
            return
        self._fade.stop()
        self._closing = True
        self._fade.setDuration(max(1, round(theme.POPUP_EXIT_DURATION_MS * self.windowOpacity())))
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _finish_fade(self) -> None:
        if self._closing:
            self.hide()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss(restore_focus=True)
            event.accept()
        else:
            super().keyPressEvent(event)

    def focusNextPrevChild(self, forward: bool) -> bool:
        if self.windowFlags() & Qt.WindowType.WindowType_Mask == Qt.WindowType.Popup:
            return cycle_focus(self, forward)
        return super().focusNextPrevChild(forward)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            paint_card(painter, QRectF(self.rect()).adjusted(6, 6, -6, -6), radius=self.radius)
        finally:
            painter.end()

    def hideEvent(self, event) -> None:
        self._fade.stop()
        self._requested = False
        self._closing = False
        if not self._anchor_pressed:
            self._watch_anchor(False)
        super().hideEvent(event)
        self.closed.emit()
        if self._return_on_hide:
            self._focus_return.restore()
        self._return_on_hide = False
