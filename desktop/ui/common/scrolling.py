"""像素滚动区的滚轮缓动；触控板保持原始位移，手动输入取消旧滑动。"""
from PySide6.QtCore import QAbstractAnimation, QEvent, QEasingCurve, QObject, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtWidgets import QAbstractItemView, QApplication, QScrollArea
from shiboken6 import isValid


class ScrollMotion(QObject):
    wheel_started = Signal(int)
    settled = Signal()
    cancelled = Signal()

    def __init__(self, area, parent=None) -> None:
        super().__init__(parent or area)
        self.area = area
        self._viewport = area.viewport()
        self._disposed = False
        if isinstance(area, QAbstractItemView):
            area.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
            area.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._bars = {"x": area.horizontalScrollBar(), "y": area.verticalScrollBar()}
        self._animations = {}
        self._targets = {}
        self._directions = {}
        for axis, bar in self._bars.items():
            animation = QPropertyAnimation(bar, b"value", self)
            animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            animation.finished.connect(lambda name=axis: self._finished(name))
            bar.sliderPressed.connect(self.stop)
            self._animations[axis] = animation
        self._pixel_idle = QTimer(self)
        self._pixel_idle.setSingleShot(True)
        self._pixel_idle.setInterval(150)
        self._pixel_idle.timeout.connect(self._emit_settled)
        self._viewport.installEventFilter(self)
        area.installEventFilter(self)
        area.destroyed.connect(self._dispose)
        self._viewport.destroyed.connect(self._dispose)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._dispose)

    def _dispose(self, *_args) -> None:
        if self._disposed:
            return
        self._disposed = True
        self.stop()
        for target in (self.area, self._viewport):
            if isValid(target) and isValid(self):
                target.removeEventFilter(self)

    @property
    def active(self) -> bool:
        return not self._disposed and ((isValid(self._pixel_idle) and self._pixel_idle.isActive()) or any(
            isValid(animation) and animation.state() == QAbstractAnimation.State.Running for animation in self._animations.values()))

    def stop(self) -> None:
        if isValid(self._pixel_idle):
            self._pixel_idle.stop()
        for animation in self._animations.values():
            if isValid(animation):
                animation.stop()
        self._targets.clear()
        self._directions.clear()

    def _finished(self, axis: str) -> None:
        self._targets.pop(axis, None)
        self._directions.pop(axis, None)
        self._emit_settled()

    def _emit_settled(self) -> None:
        if not self._disposed and not self.active:
            self.settled.emit()

    def move_to(self, value: int, *, duration: int = 180) -> None:
        if self._disposed:
            return
        self.stop()
        self._animate("y", value, duration)

    def shift_vertical(self, offset: int) -> None:
        """视口补齐高度时一起平移当前值和未结束的目标，不创造滚轮输入。"""
        if not offset or self._disposed:
            return
        target = self._targets.get("y")
        animation = self._animations.get("y")
        remaining = max(1, animation.duration() - animation.currentTime()) if animation is not None else 1
        bar = self._bars["y"]
        bar.setValue(bar.value() + offset)
        if target is not None:
            self._animate("y", target + offset, remaining)

    def _animate(self, axis: str, target: float, duration: int) -> None:
        bar = self._bars[axis]
        target = round(min(bar.maximum(), max(bar.minimum(), target)))
        animation = self._animations[axis]
        animation.stop()
        if target == bar.value():
            self._targets.pop(axis, None)
            self._emit_settled()
            return
        self._targets[axis] = target
        animation.setDuration(duration)
        animation.setStartValue(bar.value())
        animation.setEndValue(target)
        animation.start()

    def eventFilter(self, watched, event) -> bool:
        if self._disposed or not isValid(self.area) or not isValid(self._viewport):
            return False
        if event.type() in {QEvent.Type.Hide, QEvent.Type.MouseButtonPress, QEvent.Type.KeyPress}:
            was_active = self.active
            self.stop()
            if was_active and event.type() != QEvent.Type.Hide:
                self.cancelled.emit()
        if watched is not self._viewport or event.type() != QEvent.Type.Wheel:
            return False
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            return False
        pixels, angles = event.pixelDelta(), event.angleDelta()
        if pixels.isNull():
            axis, delta = ("x", angles.x()) if angles.x() else ("y", angles.y())
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                axis = "x"
            deltas = ((axis, delta),)
        else:
            deltas = (("x", pixels.x()), ("y", pixels.y()))
        if not any(delta for _axis, delta in deltas):
            handled = self.active
            if handled and event.phase() == Qt.ScrollPhase.ScrollEnd:
                self._pixel_idle.stop()
                self._emit_settled()
            # 零位移起止帧只由正在运动的区域接收，空内层不截住外层通知。
            event.setAccepted(handled)
            return True
        deltas = tuple((axis, delta) for axis, delta in deltas
                       if delta and self._bars[axis].maximum() > self._bars[axis].minimum())
        if not deltas:
            # 拦下原生处理但不接受事件：Qt沿父控件继续投递。
            # 不能return False，否则表格或只读框可能再次消费这次滚轮。
            # 有范围但已到边界仍在下方接收，保留内层独立阅读规则。
            event.ignore()
            return True
        self.wheel_started.emit(next((delta for axis, delta in deltas if axis == "y"), 0))
        if not pixels.isNull():
            self.stop()
            for axis, delta in deltas:
                bar = self._bars[axis]
                bar.setValue(bar.value() - delta)
            self._pixel_idle.start()
        else:
            self._pixel_idle.stop()
            axis, delta = deltas[0]
            bar = self._bars[axis]
            sign = 1 if delta > 0 else -1
            previous = self._targets.get(axis, bar.value()) if self._directions.get(axis) == sign else bar.value()
            self._directions[axis] = sign
            lines = QApplication.wheelScrollLines()
            extent = self._viewport.width() if axis == "x" else self._viewport.height()
            step = extent if lines < 0 else max(24, bar.singleStep()) * lines
            self._animate(axis, previous - delta / 120 * step, 140)
        event.accept()
        return True


class SmoothScrollArea(QScrollArea):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.motion = ScrollMotion(self)
