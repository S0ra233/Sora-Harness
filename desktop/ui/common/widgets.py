"""少量已有多个使用者的视觉控件：图标按钮、图标和单行省略文字。"""
from collections.abc import Callable
from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QTextCursor
from PySide6.QtWidgets import QComboBox, QLabel, QListView, QSizePolicy, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTextEdit, QToolButton, QVBoxLayout, QWidget

from .icons import icon
from ..shell import theme
from .scrolling import ScrollMotion
from .motion import HoverMotion
from .popup import CardPopup


class ChoiceDelegate(QStyledItemDelegate):
    """候选行的悬停浅于选中；文字和选中标记仍来自真实模型索引。"""

    def __init__(self, current_row: Callable[[], int], parent=None, *, check_right: bool = False) -> None:
        super().__init__(parent)
        self._current_row = current_row
        self._check_right = check_right

    def paint(self, painter, option, index) -> None:
        prepared = QStyleOptionViewItem(option)
        self.initStyleOption(prepared, index)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        selected = index.row() == self._current_row() and bool(index.flags() & Qt.ItemFlag.ItemIsSelectable)
        selectable = bool(index.flags() & Qt.ItemFlag.ItemIsSelectable)
        hovered = selectable and bool(option.state & QStyle.StateFlag.State_MouseOver)
        focused = selectable and bool(option.state & QStyle.StateFlag.State_Selected) and not hovered
        rect = option.rect.adjusted(4, 2, -4, -2)
        if selected or hovered or focused:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.SELECTED if selected else theme.HOVER))
            painter.drawRoundedRect(rect, 8, 8)
        painter.setFont(prepared.font)
        painter.setPen(QColor(theme.TEXT if selectable else theme.MUTED))
        # 所有候选都为左侧勾号预留同一宽度，切换选择时文字不横跳。
        text_rect = rect.adjusted(12 if self._check_right else 34, 0, -34 if self._check_right else -12, 0)
        text = prepared.fontMetrics.elidedText(str(index.data() or ""), Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, text)
        if selected:
            x = rect.right() - 24 if self._check_right else rect.left() + 10
            icon("check", theme.TEXT, 14).paint(painter, QRect(x, rect.center().y() - 7, 14, 14))
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        hint = super().sizeHint(option, index)
        return QSize(hint.width() + 42, max(36, hint.height() + 14))


class ClickComboBox(QComboBox):
    """数据仍归 QComboBox；白色卡片和动画不触碰 Qt 私有弹层。"""

    popup_closed = Signal()

    def __init__(self, parent=None, *, compact: bool = False) -> None:
        super().__init__(parent)
        self.setStyleSheet(theme.choice_stylesheet(compact=compact))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        view = QListView()
        view.motion = ScrollMotion(view)
        view.setMouseTracking(True)
        view.setUniformItemSizes(True)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        view.setItemDelegate(ChoiceDelegate(self.currentIndex, view))
        self.setView(view)
        self.setMaxVisibleItems(9)
        self._popup = None
        self._choices = None

    def wheelEvent(self, event) -> None:
        # 不调用QComboBox.wheelEvent，避免闭合状态下索引随滚轮改变。
        event.ignore()

    def setEditable(self, editable: bool) -> None:
        super().setEditable(editable)
        if editable:
            self.lineEdit().setFrame(False)
            self.lineEdit().installEventFilter(self)

    def _set_visual_state(self, name: str, value: bool) -> None:
        if self.property(name) != value:
            self.setProperty(name, value)
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()

    def eventFilter(self, watched, event) -> bool:
        if watched is self._choices and event.type() == QEvent.Type.KeyPress:
            if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
                self._activate_choice(self._choices.currentIndex())
                return True
            if event.key() == Qt.Key.Key_Escape:
                self._popup.dismiss(restore_focus=True)
                return True
        if watched is self.lineEdit() and event.type() in {QEvent.Type.FocusIn, QEvent.Type.FocusOut}:
            self._set_visual_state("inputFocus", event.type() == QEvent.Type.FocusIn)
            if event.type() == QEvent.Type.FocusIn:
                self._set_visual_state("keyboardFocus", event.reason() in {
                    Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason, Qt.FocusReason.ShortcutFocusReason})
        if watched is self.lineEdit() and event.type() == QEvent.Type.MouseButtonPress:
            self._set_visual_state("keyboardFocus", False)
        return super().eventFilter(watched, event)

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self._set_visual_state("keyboardFocus", event.reason() in {
            Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason, Qt.FocusReason.ShortcutFocusReason})

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        if event.reason() != Qt.FocusReason.PopupFocusReason:
            self._set_visual_state("keyboardFocus", False)

    def keyPressEvent(self, event) -> None:
        self._set_visual_state("keyboardFocus", True)
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        # 复用平台的细线图标，避免系统箭头、右侧分隔线和自绘箭头叠加。
        painter = QPainter(self)
        color = theme.MUTED if self.isEnabled() else theme.FAINT
        icon("chevron", color, 14).paint(painter, QRect(self.width() - 25, (self.height() - 14) // 2, 14, 14))
        painter.end()

    def showPopup(self) -> None:
        if not self.count():
            return
        if self._popup is None:
            self._popup = CardPopup(self, "choicePopup")
            self._popup.bind_toggle(self._toggle_popup)
            # 表格 Delegate 据父子关系判断焦点是否进入编辑器的弹层；不能挂在主窗口后误关闭编辑器。
            self._popup.setParent(self, self._popup.windowFlags())
            self._popup.setStyleSheet(theme.choice_popup_stylesheet())
            layout = QVBoxLayout(self._popup)
            layout.setContentsMargins(12, 12, 12, 12)
            self._choices = QListView(self._popup)
            self._choices.setUniformItemSizes(True)
            self._choices.setMouseTracking(True)
            self._choices.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self._choices.setItemDelegate(ChoiceDelegate(self.currentIndex, self._choices))
            self._choices.motion = ScrollMotion(self._choices)
            self._choices.installEventFilter(self)
            self._choices.clicked.connect(self._activate_choice)
            layout.addWidget(self._choices)
            self._popup.closed.connect(self._popup_hidden)
        self._choices.setModel(self.model())
        self._choices.setRootIndex(self.rootModelIndex())
        self._choices.setModelColumn(self.modelColumn())
        index = self.model().index(self.currentIndex(), self.modelColumn(), self.rootModelIndex())
        self._choices.setCurrentIndex(index)
        self._choices.scrollTo(index)
        available = self.screen().availableGeometry()
        height = min(24 + 36 * min(self.count(), self.maxVisibleItems()), available.height())
        width = min(max(120, self.width()), available.width())
        position = self.mapToGlobal(QPoint(0, self.height() + 2))
        if position.y() + height > available.bottom() + 1:
            position.setY(self.mapToGlobal(QPoint(0, -height - 2)).y())
        position.setX(max(available.left(), min(position.x(), available.right() + 1 - width)))
        position.setY(max(available.top(), position.y()))
        self._popup.setGeometry(position.x(), position.y(), width, height)
        self._set_visual_state("expanded", True)
        self._popup.present()
        self._choices.setFocus(Qt.FocusReason.PopupFocusReason)

    def hidePopup(self) -> None:
        if self._popup is not None and self._popup.isVisible():
            self._popup.dismiss()
        else:
            self._popup_hidden()

    def _popup_hidden(self) -> None:
        super().hidePopup()
        self._set_visual_state("expanded", False)
        self.popup_closed.emit()

    def _activate_choice(self, index) -> None:
        if self._popup is None or not self._popup.is_open:
            return
        if index.isValid() and index.flags() & Qt.ItemFlag.ItemIsEnabled and index.flags() & Qt.ItemFlag.ItemIsSelectable:
            self.setCurrentIndex(index.row())
            self.activated.emit(index.row())
            self.textActivated.emit(self.currentText())
            self._popup.dismiss(restore_focus=True)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._set_visual_state("keyboardFocus", False)
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._toggle_popup()
            event.accept()
            return
        super().mousePressEvent(event)

    def _toggle_popup(self) -> None:
        if self._popup is not None and self._popup.is_open:
            self.hidePopup()
        else:
            self.showPopup()

    def hideEvent(self, event) -> None:
        if self._popup is not None:
            self._popup.hide()
        super().hideEvent(event)


def highlight_text(editor, ranges: tuple[tuple[int, int], ...]) -> None:
    """正文与Shell共用Qt字符位置；在各自文档建立游标，不能跨文档使用。"""
    selections = []
    for start, end in ranges:
        cursor = QTextCursor(editor.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        selection = QTextEdit.ExtraSelection()
        selection.cursor = cursor
        selection.format.setBackground(QColor(theme.SEARCH_MATCH))
        selections.append(selection)
    editor.setExtraSelections(selections)


class IconButton(QToolButton):
    def __init__(self, name: str, title: str, parent=None, *, size: int = 32) -> None:
        super().__init__(parent)
        self.setObjectName("iconButton")
        self.setFixedSize(size, size)
        self.setIconSize(QSize(18, 18))
        self.setIcon(icon(name))
        self.setToolTip(title)
        self.setAccessibleName(title)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._motion = HoverMotion(self)
        self.setStyleSheet("QToolButton#iconButton, QToolButton#iconButton:hover, QToolButton#iconButton:focus { background: transparent; border: none; }")

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            color = QColor(theme.PRESSED if self.isDown() else theme.NAVIGATION_SELECTED if self.isChecked() else theme.HOVER)
            color.setAlphaF(1.0 if self.isDown() or self.isChecked() else self._motion.progress)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            rect = self.rect().adjusted(1, 1, -1, -1)
            radius = 12 if self.isCheckable() and self.width() >= 38 else 8
            painter.drawRoundedRect(rect, radius, radius)
            size = self.iconSize()
            target = QRect((self.width() - size.width()) // 2, (self.height() - size.height()) // 2, size.width(), size.height())
            painter.translate(0, self._motion.press_offset)
            self.icon().paint(painter, target, Qt.AlignmentFlag.AlignCenter,
                              QIcon.Mode.Normal if self.isEnabled() else QIcon.Mode.Disabled)
            painter.translate(0, -self._motion.press_offset)
            if self._motion.keyboard_focus:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(theme.CONTROL_BORDER), 1))
                painter.drawRoundedRect(rect, radius, radius)
        finally:
            painter.end()


class IconGlyph(QWidget):
    def __init__(self, name: str, parent=None, *, size: int = 18, color: str = theme.MUTED) -> None:
        super().__init__(parent)
        self._icon = icon(name, color, size)
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        self._icon.paint(painter, self.rect())


class ElidedLabel(QLabel):
    """保留完整原文供提示使用；缩窗时只改变显示，不修改标题／模型状态。"""

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(parent)
        self.full_text = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:
        self.full_text = text
        self._refresh_text()
        self.updateGeometry()

    def _refresh_text(self) -> None:
        super().setText(self.fontMetrics().elidedText(self.full_text, Qt.TextElideMode.ElideRight, self.contentsRect().width()))

    def sizeHint(self) -> QSize:
        return QSize(self.fontMetrics().horizontalAdvance(self.full_text), self.fontMetrics().height() + 4)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.fontMetrics().height() + 4)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_text()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in {QEvent.Type.FontChange, QEvent.Type.StyleChange}:
            self._refresh_text()
            self.updateGeometry()
