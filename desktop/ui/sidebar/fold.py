"""侧栏分组的可见区收展；不逐帧修改模型或重新排列目录行。"""
from PySide6.QtCore import QAbstractAnimation, QEvent, QEasingCurve, QObject, QPoint, QPersistentModelIndex, QRect, Qt, Signal, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from ..shell import theme
from .model import KIND_ROLE


class _FoldLayer(QWidget):
    toggle_requested = Signal(object)
    interaction_started = Signal()
    interaction_finished = Signal()

    def __init__(self, tree) -> None:
        super().__init__(tree.viewport())
        self.tree = tree
        # 此层逐帧填满实色背景，Qt无需在它下面反复重画真实目录行。
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.before = QPixmap()
        self.after = QPixmap()
        self.top = 0
        self.start_extent = self.extent = 0.0
        self.expanding = True
        self.before_headers = self.after_headers = ()
        self._pressed = QPersistentModelIndex()
        self.hide()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setClipRegion(event.region())
            painter.fillRect(self.rect(), QColor(theme.SIDEBAR))
            shown = round(self.extent)
            painter.save()
            painter.setClipRect(QRect(0, 0, self.width(), shown), Qt.ClipOperation.IntersectClip)
            painter.drawPixmap(0, -self.top, self.after if self.expanding else self.before)
            painter.restore()
            painter.setClipRect(QRect(0, shown, self.width(), max(0, self.height() - shown)), Qt.ClipOperation.IntersectClip)
            offset = shown - round(self.start_extent) if self.expanding else shown
            painter.drawPixmap(0, offset - self.top, self.before if self.expanding else self.after)
        finally:
            painter.end()

    def mousePressEvent(self, event) -> None:
        self._pressed = self._header_at(event.position().toPoint()) if event.button() == Qt.MouseButton.LeftButton else QPersistentModelIndex()
        if self._pressed.isValid():
            # 按下时固定命中的标题，松开前动画不能隐藏它并吞掉这次操作。
            self.interaction_started.emit()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        pressed, self._pressed = self._pressed, QPersistentModelIndex()
        if event.button() == Qt.MouseButton.LeftButton and pressed.isValid() and pressed == self._header_at(event.position().toPoint()):
            self.toggle_requested.emit(pressed)
        self.interaction_finished.emit()
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        self.mousePressEvent(event)

    def visible_headers(self):
        """快照移动时带着标题的稳定索引移动，不用真实树的新坐标猜用户点了谁。"""
        shown = round(self.extent)
        before, after = self.before_headers, self.after_headers
        offset = shown - round(self.start_extent) if self.expanding else shown
        regions = ((after if self.expanding else before, 0, QRect(0, self.top, self.width(), shown)),
                   (before if self.expanding else after, offset, QRect(0, self.top + shown, self.width(), max(0, self.height() - shown))))
        result = []
        for headers, shift, area in regions:
            for index, rect, visible in headers:
                rect, visible = rect.translated(0, shift), visible.translated(0, shift).intersected(area)
                if not visible.isEmpty():
                    result.append((index, rect, visible))
        return tuple(result)

    def _header_at(self, point):
        point += QPoint(0, self.top)
        for index, rect, visible in self.visible_headers():
            if index.isValid() and visible.contains(point) and self.tree.itemDelegate().action_at(index, rect, point) is None:
                return index
        return QPersistentModelIndex()

    def contextMenuEvent(self, event) -> None:
        event.accept()


class SidebarFold(QObject):
    """原树视图持有真实展开状态；此对象只负责80ms的显示过渡和资源关闭。"""

    def __init__(self, tree) -> None:
        super().__init__(tree)
        self.tree = tree
        self._viewport = tree.viewport()
        self._disposed = False
        self._index = QPersistentModelIndex()
        self._end_extent = 0
        self._full_extent = 1
        self._progress = 0.0
        self._layer = _FoldLayer(tree)
        self._layer.toggle_requested.connect(self.toggle)
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(theme.SIDEBAR_GROUP_DURATION_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._tick)
        self._animation.finished.connect(self._finish)
        self._layer.interaction_started.connect(self._animation.stop)
        self._layer.interaction_finished.connect(self._resume)
        tree.installEventFilter(self)
        self._viewport.installEventFilter(self)
        tree.verticalScrollBar().valueChanged.connect(self.stop)
        tree.destroyed.connect(self._dispose)
        self._viewport.destroyed.connect(self._dispose)
        self._layer.destroyed.connect(self._dispose)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._dispose)

    def _dispose(self, *_args) -> None:
        if self._disposed:
            return
        self._disposed = True
        if isValid(self._animation):
            self._animation.stop()
        for target in (self.tree, self._viewport):
            if isValid(target) and isValid(self):
                target.removeEventFilter(self)

    def stop(self, *_args) -> None:
        if self._disposed:
            return
        self._animation.stop()
        self._layer.hide()
        self._layer.before = QPixmap()
        self._layer.after = QPixmap()
        self._layer.before_headers = self._layer.after_headers = ()
        self._layer._pressed = QPersistentModelIndex()
        self._index = QPersistentModelIndex()

    def _finish(self) -> None:
        if self._disposed:
            return
        self.stop()
        self.tree.update_hover()

    def _resume(self) -> None:
        if self._disposed:
            return
        if self._layer.isVisible() and self._animation.state() != QAbstractAnimation.State.Running:
            progress = self._progress
            remaining = abs(self._end_extent - self._layer.extent)
            self._animation.setDuration(max(1, round(theme.SIDEBAR_GROUP_DURATION_MS * remaining / self._full_extent)))
            self._animation.setStartValue(progress)
            self._animation.setEndValue(1.0)
            self._animation.start()

    def _child_extent(self, index, maximum: int) -> int:
        height = 0
        child = self.tree.indexBelow(index)
        while child.isValid() and height < maximum:
            parent = child.parent()
            while parent.isValid() and parent != index:
                parent = parent.parent()
            if not parent.isValid():
                break
            height += self.tree.visualRect(child).height()
            child = self.tree.indexBelow(child)
        return min(height, maximum)

    def _headers(self):
        viewport = self.tree.viewport()
        headers = []
        index = self.tree.indexAt(QPoint(1, 0))
        while index.isValid():
            rect = self.tree.visualRect(index)
            if rect.top() >= viewport.height():
                break
            if index.data(KIND_ROLE) in {"project", "section"} and (not self._layer.isVisible() or rect.bottom() < self._layer.top):
                headers.append((QPersistentModelIndex(index), rect, rect.intersected(viewport.rect())))
            index = self.tree.indexBelow(index)
        if self._layer.isVisible():
            headers.extend(self._layer.visible_headers())
        return tuple(headers)

    def toggle(self, index) -> None:
        if self._disposed or not index.isValid():
            return
        viewport = self.tree.viewport()
        expanded = self.tree.isExpanded(index)
        top = self.tree.visualRect(index).bottom() + 1
        available = max(0, viewport.height() - top)
        same = self._layer.isVisible() and self._index == index
        full_extent = self._child_extent(index, available) if expanded else 0
        if same:
            start = self._layer.extent
        else:
            start = full_extent
        if not self.tree.isVisible() or self.tree.visualRect(index).isEmpty() or top < 0 or available == 0:
            self.stop()
            self.tree.setExpanded(index, not expanded)
            return
        # 反向点击先保留当前呈现的画面；不用旧动画起点突然重放。
        before = viewport.grab()
        before_headers = self._headers()
        position = self.tree.verticalScrollBar().value()
        self.stop()
        self.tree.setExpanded(index, not expanded)
        self.tree.doItemsLayout()
        if position != self.tree.verticalScrollBar().value() or top != self.tree.visualRect(index).bottom() + 1:
            # 原生滚动范围收缩导致位置变化时直接显示真实结果，不能移动一张坐标失效的快照。
            self.tree.update_hover()
            return
        end = 0 if expanded else self._child_extent(index, available)
        if start == end:
            self.tree.update_hover()
            return
        self._layer.before, self._layer.after = before, viewport.grab()
        self._layer.before_headers, self._layer.after_headers = before_headers, self._headers()
        self._layer.top = top
        self._layer.start_extent = self._layer.extent = start
        self._layer.expanding = not expanded
        self._end_extent = end
        self._index = QPersistentModelIndex(index)
        self._layer.setGeometry(0, top, viewport.width(), available)
        self._layer.show()
        self._layer.raise_()
        self._progress = 0.0
        self._full_extent = max(1, full_extent, start, end)
        self._animation.setDuration(max(1, round(theme.SIDEBAR_GROUP_DURATION_MS * abs(end - start) / self._full_extent)))
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.start()

    def _tick(self, value) -> None:
        if self._disposed:
            return
        self._progress = float(value)
        self._layer.extent = self._layer.start_extent + (self._end_extent - self._layer.start_extent) * float(value)
        self._layer.update()

    def eventFilter(self, watched, event) -> bool:
        if self._disposed or not isValid(self.tree) or not isValid(self._viewport):
            return False
        if event.type() == QEvent.Type.KeyPress and event.key() in {
            Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Plus, Qt.Key.Key_Minus,
        }:
            return False
        if event.type() in {QEvent.Type.Hide, QEvent.Type.Resize, QEvent.Type.KeyPress, QEvent.Type.Wheel}:
            self.stop()
        return False
