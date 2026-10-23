"""页面切换只淡入新页面，不改变页面或后台会话的生命周期。"""
from PySide6.QtCore import QEasingCurve, QPropertyAnimation
from PySide6.QtWidgets import QStackedWidget

from .surfaces import FadeVeil
from ..shell import theme


class FadingStack(QStackedWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._veil = FadeVeil(self)
        self._fade = QPropertyAnimation(self._veil, b"opacity", self)
        self._fade.setDuration(theme.PAGE_FADE_DURATION_MS)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self._veil.hide)
        self.currentChanged.connect(self._show_page)

    def _show_page(self, _index: int) -> None:
        self._fade.stop()
        if not self.isVisible():
            self._veil.hide()
            return
        self._veil.setGeometry(self.rect())
        self._veil.opacity = 1.0
        self._veil.show()
        self._veil.raise_()
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.start()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._veil.setGeometry(self.rect())

    def hideEvent(self, event) -> None:
        self._fade.stop()
        self._veil.hide()
        super().hideEvent(event)
