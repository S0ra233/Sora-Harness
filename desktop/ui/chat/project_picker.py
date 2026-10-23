"""欢迎页项目入口；候选只传项目 ID，保存和目录授权仍由工作区处理。"""
from PySide6.QtCore import QEvent, QPoint, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QFrame, QListWidget, QListWidgetItem, QToolButton, QVBoxLayout, QWidget

from ..common.icons import icon
from ..common.motion import HoverMotion
from ..common.popup import CardPopup
from ..common.scrolling import ScrollMotion
from ..common.widgets import ChoiceDelegate
from ..shell import theme


class ScopeTray(QWidget):
    """上缘藏在白色输入卡后，下方留下有圆角的灰色托盘。"""

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.CONTROL))
            painter.drawRoundedRect(QRectF(0, -16, self.width(), self.height() + 16), 16, 16)
        finally:
            painter.end()


class ProjectPicker(QToolButton):
    activated = Signal(object)
    popup_closed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("projectPicker")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("QToolButton { background: transparent; border: none; }")
        self.setFixedHeight(30)
        self._motion = HoverMotion(self)
        self._selected = None
        self._choice_committed = False
        self._popup = CardPopup(self, "projectMenu")
        self._popup.bind_toggle(self._toggle)
        self._list = QListWidget()
        self._list.setStyleSheet(f"QListWidget {{ background: {theme.BACKGROUND}; color: {theme.TEXT}; border: none; outline: none; font-size: 12px; }}"
                                "QListWidget::item { background: transparent; border: none; }")
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setMouseTracking(True)
        self._list.setItemDelegate(ChoiceDelegate(self._selected_row, self._list, check_right=True))
        self._list.installEventFilter(self)
        self._list.motion = ScrollMotion(self._list)
        layout = QVBoxLayout(self._popup)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(self._list)
        self._list.itemClicked.connect(self._choose)
        self._popup.closed.connect(self.popup_closed.emit)
        self.clicked.connect(self._toggle)
        self.set_project(None)

    @property
    def popup_visible(self) -> bool:
        return self._popup.is_open

    def _selected_row(self) -> int:
        return next((row for row in range(self._list.count())
                     if self._list.item(row).data(Qt.ItemDataRole.UserRole) == self._selected), -1)

    def set_projects(self, projects) -> None:
        self._list.clear()
        for project in projects:
            item = QListWidgetItem(project.name, self._list)
            item.setData(Qt.ItemDataRole.UserRole, project.project_id)
        item = QListWidgetItem("创建项目…", self._list)
        item.setData(Qt.ItemDataRole.UserRole, "")
        height = min(330, max(60, self._list.count() * 36 + 24))
        self._popup.setFixedSize(280, height)
        self.set_project(self._selected)

    def set_project(self, project_id) -> None:
        self._selected = project_id
        row = self._selected_row()
        self.setText(self._list.item(row).text() if row >= 0 else "选择项目")
        self.setAccessibleName(self.text())
        if row >= 0:
            self._list.setCurrentRow(row)
        self._list.viewport().update()
        self.updateGeometry()

    def _toggle(self) -> None:
        if self._popup.is_open:
            self._popup.dismiss(restore_focus=True)
        else:
            self.showPopup()

    def showPopup(self) -> None:
        self._choice_committed = False
        point = self.mapToGlobal(QPoint(-6, self.height() + 5))
        area = self.screen().availableGeometry()
        x = min(max(point.x(), area.left()), max(area.left(), area.right() + 1 - self._popup.width()))
        y = point.y() if point.y() + self._popup.height() <= area.bottom() + 1 else self.mapToGlobal(QPoint(0, -self._popup.height() - 5)).y()
        self._popup.move(x, max(area.top(), y))
        self._popup.present()
        self._list.setFocus()

    def _choose(self, item) -> None:
        if item is None or self._choice_committed or not self.popup_visible:
            return
        self._choice_committed = True
        self._popup.dismiss(restore_focus=True)
        self.activated.emit(item.data(Qt.ItemDataRole.UserRole) or None)

    def eventFilter(self, watched, event) -> bool:
        if watched is self._list and event.type() == QEvent.Type.KeyPress and event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            self._choose(self._list.currentItem())
            return True
        return super().eventFilter(watched, event)

    def sizeHint(self) -> QSize:
        return QSize(min(300, max(110, self.fontMetrics().horizontalAdvance(self.text()) + 32)), 30)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            color = QColor(theme.PRESSED if self.isDown() else theme.HOVER)
            color.setAlphaF(1.0 if self.isDown() else self._motion.progress)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(self.rect()), 8, 8)
            painter.setFont(self.font())
            painter.setPen(QColor(theme.TEXT))
            rect = self.rect().adjusted(6, 0, -24, 0)
            painter.drawText(rect, Qt.AlignmentFlag.AlignVCenter, self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, rect.width()))
            icon("chevron", theme.MUTED, 12).paint(painter, self.rect().adjusted(self.width() - 20, 9, -8, -9))
        finally:
            painter.end()
