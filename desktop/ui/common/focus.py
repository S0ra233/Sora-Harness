"""浮层与辅助页面共用的焦点返回；不抢夺其他窗口或新弹层的焦点。"""
import weakref

from PySide6.QtCore import QObject, QTimer, Qt, Slot
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid


class FocusReturn(QObject):
    def __init__(self, owner) -> None:
        super().__init__(owner)
        self._target = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._restore)

    def capture(self, fallback=None) -> None:
        self._timer.stop()
        target = QApplication.focusWidget()
        if target is None:
            target = fallback
        self._target = weakref.ref(target) if target is not None else None

    def restore(self) -> None:
        # 等Qt完成隐藏和窗口激活；新浮层先打开时，本次返回自动放弃。
        self._timer.start(0)

    @Slot()
    def _restore(self) -> None:
        target = self._target() if self._target is not None else None
        if (target is None or not isValid(target) or not target.isVisible() or not target.isEnabled()
                or QApplication.activePopupWidget() is not None or not target.window().isActiveWindow()):
            return
        target.setFocus(Qt.FocusReason.OtherFocusReason)


def cycle_focus(scope, forward: bool) -> bool:
    """弹层内Tab循环仅访问可交互子控件，不越到背后的主页面。"""
    current = QApplication.focusWidget() or scope
    if current is not scope and not scope.isAncestorOf(current):
        current = scope
    widget = current
    while True:
        widget = widget.nextInFocusChain() if forward else widget.previousInFocusChain()
        if widget is current:
            return scope.isAncestorOf(current)
        if (scope.isAncestorOf(widget) and widget.isVisibleTo(scope) and widget.isEnabled()
                and widget.focusPolicy() & Qt.FocusPolicy.TabFocus):
            widget.setFocus(Qt.FocusReason.TabFocusReason if forward else Qt.FocusReason.BacktabFocusReason)
            return True
