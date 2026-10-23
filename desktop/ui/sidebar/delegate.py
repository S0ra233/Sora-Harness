"""绘制侧栏单行与局部动作；不为每个会话创建常驻按钮和定时器。"""
from PySide6.QtCore import QPoint, QPersistentModelIndex, QRect, QSize, Qt, Signal, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QToolTip

from ..shell import theme
from ..common.icons import icon
from .model import ACTIVE_ROLE, BUSY_ROLE, ID_ROLE, KIND_ROLE, PIN_ROLE


class SidebarDelegate(QStyledItemDelegate):
    action_requested = Signal(str, str)
    repaint_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.angle = 0
        self.hover_index = QPersistentModelIndex()
        self._opacity = {}
        self._starts = {}
        self._hover_animation = QVariantAnimation(self)
        self._hover_animation.setDuration(theme.HOVER_DURATION_MS)
        self._hover_animation.valueChanged.connect(self._hover_tick)

    def set_hover(self, index) -> None:
        target = QPersistentModelIndex(index)
        if target == self.hover_index:
            return
        self._hover_animation.stop()
        self.hover_index = target
        self._starts = dict(self._opacity)
        if target.isValid():
            self._starts.setdefault(target, 0.0)
        self._hover_animation.setStartValue(0.0)
        self._hover_animation.setEndValue(1.0)
        self._hover_animation.start()

    def _hover_tick(self, value) -> None:
        fraction = float(value)
        self._opacity = {index: start + ((1.0 if index == self.hover_index else 0.0) - start) * fraction
                         for index, start in self._starts.items() if index.isValid()}
        self._opacity = {index: opacity for index, opacity in self._opacity.items() if opacity > 0.001}
        self.repaint_requested.emit()

    def sizeHint(self, option, index) -> QSize:
        return QSize(240, 40 if index.data(KIND_ROLE) == "section" else 32)

    @staticmethod
    def _actions(rect: QRect, kind: str, identifier: str) -> tuple[tuple[str, QRect], ...]:
        last = QRect(rect.right() - 27, rect.center().y() - 12, 24, 24)
        if kind == "conversation":
            return (("more", last.translated(-26, 0)), ("pin", last))
        if kind == "project" or (kind == "section" and identifier == "projects"):
            return (("new", last),)
        return ()

    def paint(self, painter: QPainter, option, index) -> None:
        kind, identifier = index.data(KIND_ROLE), index.data(ID_ROLE)
        hovered = index == self.hover_index
        opacity = self._opacity.get(QPersistentModelIndex(index), 0.0)
        active = bool(index.data(ACTIVE_ROLE))
        rect = option.rect.adjusted(2, 1, -4, -1)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if kind == "conversation" and (opacity > 0 or active):
            painter.setPen(Qt.PenStyle.NoPen)
            base, hover = QColor(theme.SIDEBAR), QColor(theme.HOVER)
            background = QColor(theme.SIDEBAR_SELECTED) if active else QColor(
                round(base.red() + (hover.red() - base.red()) * opacity),
                round(base.green() + (hover.green() - base.green()) * opacity),
                round(base.blue() + (hover.blue() - base.blue()) * opacity))
            painter.setBrush(background)
            painter.drawRoundedRect(rect, 9, 9)
        font = option.font
        font.setBold(False)
        font.setFamilies(theme.TEXT_FAMILIES)
        font.setPixelSize(12 if kind == "section" else theme.BODY_FONT_SIZE)
        painter.setFont(font)
        painter.setPen(QColor(theme.MUTED if kind == "section" else theme.TEXT))
        left = rect.left() + 11
        if kind == "project":
            icon("folder", theme.MUTED, 16).paint(painter, QRect(left, rect.center().y() - 8, 16, 16))
            left += 23
        elif kind == "conversation" and index.parent().data(KIND_ROLE) == "project":
            # 分组只是数据组织；项目内聊天与项目名称对齐，不重复叠加树层级缩进。
            left += 23
        actions = self._actions(rect, kind, identifier)
        show_actions = opacity > 0
        right = actions[0][1].left() - 3 if actions and show_actions else rect.right() - 8
        if kind == "section" and hovered:
            arrow_x = min(left + painter.fontMetrics().horizontalAdvance(index.data()) + 5, right - 14)
            name = "chevron" if option.state & QStyle.StateFlag.State_Open else "right"
            icon(name, theme.MUTED, 12).paint(painter, QRect(arrow_x, rect.center().y() - 6, 12, 12))
        busy = bool(index.data(BUSY_ROLE))
        metrics = painter.fontMetrics()
        text = metrics.elidedText(index.data(), Qt.TextElideMode.ElideRight, max(0, right - left - (22 if busy else 0)))
        painter.drawText(QRect(left, rect.top(), max(0, right - left), rect.height()), Qt.AlignmentFlag.AlignVCenter, text)
        if busy:
            x = min(left + metrics.horizontalAdvance(text) + 8, right - 15)
            painter.setPen(QPen(QColor(theme.MUTED), 1.6))
            painter.drawArc(QRect(x, rect.center().y() - 6, 12, 12), self.angle * 16, 255 * 16)
        if show_actions:
            painter.setOpacity(opacity)
            for action, target in actions:
                self._paint_action(painter, action, target, bool(index.data(PIN_ROLE)))
        painter.restore()

    @staticmethod
    def _paint_action(painter: QPainter, action: str, rect: QRect, pinned: bool) -> None:
        name = {"more": "more", "rename": "new", "pin": "pin-filled" if pinned else "pin", "archive": "archive"}.get(action, "plus")
        color = theme.TEXT if action == "pin" and pinned else theme.MUTED
        icon(name, color, 14).paint(painter, rect.adjusted(5, 5, -5, -5))

    def action_at(self, index, rect: QRect, point: QPoint) -> str | None:
        if index != self.hover_index or self._opacity.get(QPersistentModelIndex(index), 0.0) <= 0:
            return None
        for action, target in self._actions(rect.adjusted(2, 1, -4, -1), index.data(KIND_ROLE), index.data(ID_ROLE)):
            if target.contains(point):
                return action
        return None

    def helpEvent(self, event, view, option, index) -> bool:
        action = self.action_at(index, option.rect, event.pos())
        if action is not None:
            text = "更多选项" if action == "more" else "取消置顶聊天" if action == "pin" and index.data(PIN_ROLE) else "置顶聊天" if action == "pin" else "新建项目对话"
            QToolTip.showText(event.globalPos(), text, view.viewport())
            return True
        return super().helpEvent(event, view, option, index)
