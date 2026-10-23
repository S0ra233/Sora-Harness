"""设置区和工具详情共用收展动画；状态归调用者，动画只改变显示。"""
from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QEvent, Property, QPropertyAnimation, Signal
from PySide6.QtWidgets import QLayout, QSizePolicy, QVBoxLayout, QWidget

from ..shell import theme


class AnimatedDisclosure(QWidget):
    geometry_changed = Signal()
    settled = Signal()

    def __init__(self, content: QWidget, parent=None) -> None:
        super().__init__(parent)
        self.content = content
        self._expanded = False
        self._natural_extent = 0
        self._preparing = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        # 内容在关闭动画中仍然存在，但不能强迫外壳保持其最小高度。
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.addWidget(content)
        # 收展只裁切高度，避免详情嵌套时叠加离屏效果而重复开启绘制器。
        self._animation = QPropertyAnimation(self, b"extent", self)
        self._animation.setDuration(theme.DISCLOSURE_DURATION_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.finished.connect(self._finish)
        content.installEventFilter(self)
        self.extent = 0
        self.hide()

    def _natural_height(self) -> int:
        self.content.ensurePolished()
        layout = self.content.layout()
        if layout is not None:
            layout.activate()
        if self.content.minimumHeight() == self.content.maximumHeight():
            return self.content.minimumHeight()
        hint = self.content.heightForWidth(self.width()) if self.content.hasHeightForWidth() else self.content.sizeHint().height()
        return max(0, hint, self.content.minimumSizeHint().height())

    @Property(int)
    def extent(self) -> int:
        return self.maximumHeight()

    @extent.setter
    def extent(self, value: int) -> None:
        self.setFixedHeight(max(0, value))
        self.geometry_changed.emit()

    def set_expanded(self, expanded: bool, *, animate: bool = True) -> None:
        start = self.extent
        previous = self._expanded
        self._expanded = expanded
        # 首次show引起的resize不能提前把高度填满，否则第一次展开会跳过动画。
        self._preparing = True
        try:
            if expanded:
                self.show()
                self.content.show()
                self._natural_extent = self._natural_height()
            # 收起只需当前高度，不再次排版整份详情；连续点击可以立即反向。
            natural = self._natural_extent if expanded else max(self._natural_extent, start)
            end = natural if expanded else 0
        finally:
            self._preparing = False
        if (animate and previous == expanded and self._animation.state() == QAbstractAnimation.State.Running
                and self._animation.endValue() == end):
            return
        self._animation.stop()
        if not animate or not self.isVisible():
            self.extent = end
            self._finish()
            return
        if start == end:
            self._finish()
            return
        # 反向从当前高度接续，剩余距离短时不重新播放完整时长。
        full = max(1, natural, start, end)
        self._animation.setDuration(max(1, round(theme.DISCLOSURE_DURATION_MS * abs(end - start) / full)))
        self._animation.setStartValue(start)
        self._animation.setEndValue(end)
        self._animation.start()

    def refresh_height(self) -> None:
        if not self._expanded or self._preparing:
            return
        target = self._natural_height()
        if self._animation.state() == QAbstractAnimation.State.Running:
            if self._animation.endValue() != target:
                self.set_expanded(True)
        elif self.extent != target:
            self.extent = target

    def finish_animation(self) -> None:
        """搜索定位需要确定几何尺寸，再逐层计算命中位置。"""
        self._animation.stop()
        self.extent = self._natural_height() if self._expanded else 0
        self._finish()

    def _finish(self) -> None:
        self.setVisible(self._expanded)
        self.geometry_changed.emit()
        self.settled.emit()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.content and event.type() == QEvent.Type.LayoutRequest:
            self.refresh_height()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self.refresh_height()

    def hideEvent(self, event) -> None:
        if self._animation.state() == QAbstractAnimation.State.Running:
            self._animation.stop()
            self.extent = self._natural_height() if self._expanded else 0
        super().hideEvent(event)
