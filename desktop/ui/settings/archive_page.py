"""归档列表只展示目录元数据；查看历史、恢复和界面删除分别发出意图。"""
from datetime import datetime

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QPainter, QPen, QStandardItem, QStandardItemModel
from ..common.buttons import MotionButton
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QStyle, QStyledItemDelegate, QTreeView

from ...application.catalog import CatalogSnapshot
from ..common.icons import icon
from ..common.widgets import ClickComboBox
from ..common.surfaces import CardSurface, WhiteSurface, settings_column
from ..sidebar.controller import CatalogController
from ..shell import theme


ENTRY_ROLE = int(Qt.ItemDataRole.UserRole) + 1
COUNT_ROLE = ENTRY_ROLE + 1
DATE_ROLE = ENTRY_ROLE + 2


def _date(value: str) -> str:
    try:
        stamp = datetime.fromisoformat(value).astimezone()
        return f"{stamp.year}年{stamp.month}月{stamp.day}日，{stamp:%H:%M}"
    except ValueError:
        return value


class _ArchiveDelegate(QStyledItemDelegate):
    @staticmethod
    def actions(rect: QRect):
        delete = QRect(rect.right() - 65, rect.center().y() - 17, 56, 34)
        restore = QRect(delete.left() - 98, delete.top(), 88, 34)
        return (("restore", restore), ("delete", delete))

    def sizeHint(self, option, index) -> QSize:
        return QSize(300, 78 if index.data(ENTRY_ROLE) is not None else 54)

    def paint(self, painter: QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(2, 2, -8, -2)
        entry = index.data(ENTRY_ROLE)
        painter.setFont(option.font)
        if entry is None:
            icon("folder", theme.TEXT, 18).paint(painter, QRect(rect.left() + 2, rect.center().y() - 9, 18, 18))
            painter.setPen(QColor(theme.TEXT))
            painter.drawText(rect.adjusted(30, 0, -85, 0), Qt.AlignmentFlag.AlignVCenter, str(index.data()))
            painter.setPen(QColor(theme.MUTED))
            painter.drawText(rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{index.data(COUNT_ROLE)}个聊天")
        else:
            hovered = bool(option.state & (QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_HasFocus))
            painter.setPen(QPen(QColor(theme.BORDER), 1))
            painter.setBrush(QColor(theme.HOVER if hovered else theme.BACKGROUND))
            painter.drawRoundedRect(rect, 14, 14)
            actions = self.actions(rect)
            title_rect = QRect(rect.left() + 16, rect.top() + 11, max(0, actions[0][1].left() - rect.left() - 28), 24)
            painter.setPen(QColor(theme.TEXT))
            painter.drawText(title_rect, Qt.AlignmentFlag.AlignVCenter,
                             painter.fontMetrics().elidedText(entry.title, Qt.TextElideMode.ElideRight, title_rect.width()))
            font = option.font
            font.setPixelSize(12)
            painter.setFont(font)
            painter.setPen(QColor(theme.MUTED))
            painter.drawText(title_rect.translated(0, 26), Qt.AlignmentFlag.AlignVCenter,
                             painter.fontMetrics().elidedText(index.data(DATE_ROLE), Qt.TextElideMode.ElideRight, title_rect.width()))
            for action, target in actions:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#fbecee" if action == "delete" else theme.CONTROL))
                painter.drawRoundedRect(target, 10, 10)
                painter.setPen(QColor(theme.ERROR if action == "delete" else theme.TEXT))
                painter.drawText(target, Qt.AlignmentFlag.AlignCenter, "删除" if action == "delete" else "取消归档")
        painter.restore()

    def action_at(self, index, rect: QRect, point) -> str | None:
        if index.data(ENTRY_ROLE) is not None:
            for action, target in self.actions(rect.adjusted(2, 2, -8, -2)):
                if target.contains(point):
                    return action
        return None


class _ArchiveList(QTreeView):
    action_requested = Signal(str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pressed = None

    def mousePressEvent(self, event) -> None:
        self._pressed = None
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and event.button() == Qt.MouseButton.LeftButton:
            action = self.itemDelegate().action_at(index, self.visualRect(index), event.position().toPoint())
            if action is not None:
                self._pressed = (action, index.data(ENTRY_ROLE).conversation_id)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        pressed, self._pressed = self._pressed, None
        if pressed is None:
            super().mouseReleaseEvent(event)
            return
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and index.data(ENTRY_ROLE) is not None and event.button() == Qt.MouseButton.LeftButton:
            action = self.itemDelegate().action_at(index, self.visualRect(index), event.position().toPoint())
            if pressed == (action, index.data(ENTRY_ROLE).conversation_id):
                self.action_requested.emit(*pressed)
        event.accept()


class ArchivePage(WhiteSurface):
    conversation_requested = Signal(str)

    def __init__(self, catalog: CatalogController, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("archivePage")
        self._catalog = catalog
        self._snapshot = catalog.snapshot()
        self._build()
        catalog.catalog_changed.connect(self._set_snapshot)
        catalog.state_changed.connect(self._set_state)
        self._set_snapshot(self._snapshot)
        self._set_state(catalog.state)

    def _build(self) -> None:
        layout = settings_column(self)
        title = QLabel("已归档的聊天")
        title.setObjectName("settingsHeading")
        layout.addWidget(title)
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索已归档的聊天")
        self._search.setClearButtonEnabled(True)
        layout.addWidget(self._search)
        filters = QHBoxLayout()
        self._type = ClickComboBox()
        for label, value in (("全部聊天", "all"), ("普通聊天", "plain"), ("项目聊天", "project")):
            self._type.addItem(label, value)
        self._project = ClickComboBox()
        filters.addWidget(self._type, 1)
        filters.addWidget(self._project, 1)
        layout.addLayout(filters)
        self._model = QStandardItemModel(self)
        self._list = _ArchiveList()
        self._list.setObjectName("archiveList")
        self._list.setModel(self._model)
        self._list.setItemDelegate(_ArchiveDelegate(self._list))
        self._list.setHeaderHidden(True)
        self._list.setRootIsDecorated(False)
        self._list.setIndentation(0)
        self._list.setMouseTracking(True)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setExpandsOnDoubleClick(False)
        self._list.setAnimated(True)
        self._list_card = CardSurface(self._list)
        layout.addWidget(self._list_card, 1)
        self._empty = QLabel("尚无已归档聊天。")
        self._empty.setObjectName("settingsNote")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._empty)
        footer = QHBoxLayout()
        self._status = QLabel()
        self._status.setObjectName("settingsNote")
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setWordWrap(True)
        self._retry = MotionButton("重试保存目录", variant="secondary")
        self._retry.clicked.connect(self._catalog.refresh)
        footer.addWidget(self._status, 1)
        footer.addWidget(self._retry)
        layout.addLayout(footer)
        self._search.textChanged.connect(self._filter)
        self._type.currentIndexChanged.connect(self._filter)
        self._project.currentIndexChanged.connect(self._filter)
        self._list.clicked.connect(self._open)
        self._list.activated.connect(self._open)
        self._list.action_requested.connect(self._action)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        # 键盘入口与行内按钮使用同一应用边界，不依赖鼠标坐标。
        for name, method in (("取消归档", self._catalog.restore), ("删除", self._catalog.delete_archived)):
            action = QAction(name, self._list)
            action.triggered.connect(lambda _checked=False, operation=method: self._selected_action(operation))
            self._list.addAction(action)

    def _set_snapshot(self, snapshot: CatalogSnapshot) -> None:
        visible = tuple(entry for entry in snapshot.conversations if entry.archived and not entry.deleted)
        previous = tuple(entry for entry in self._snapshot.conversations if entry.archived and not entry.deleted)
        unchanged = visible == previous and snapshot.projects == self._snapshot.projects and self._project.count() > 0
        self._snapshot = snapshot
        if unchanged:
            return
        selected = self._project.currentData() if self._project.count() else "all"
        self._project.blockSignals(True)
        self._project.clear()
        self._project.addItem("所有项目", "all")
        self._project.addItem("无项目", None)
        for project in snapshot.projects:
            self._project.addItem(project.name, project.project_id)
        index = self._project.findData(selected)
        self._project.setCurrentIndex(index if index >= 0 else 0)
        self._project.blockSignals(False)
        self._filter()

    def _filter(self, *_args) -> None:
        query, kind, project_id = self._search.text().strip().casefold(), self._type.currentData(), self._project.currentData()
        entries = [entry for entry in self._snapshot.conversations if entry.archived and not entry.deleted
                   and query in entry.title.casefold()
                   and (kind == "all" or (entry.project_id is None) == (kind == "plain"))
                   and (project_id == "all" or entry.project_id == project_id)]
        selected = self._list.currentIndex().data(ENTRY_ROLE)
        scroll = self._list.verticalScrollBar().value()
        self._model.clear()
        groups = {}
        for entry in entries:
            groups.setdefault(entry.project_id, []).append(entry)
        names = {project.project_id: project.name for project in self._snapshot.projects}
        for group_id, members in groups.items():
            group = QStandardItem(names.get(group_id, "无项目"))
            group.setFlags(Qt.ItemFlag.ItemIsEnabled)
            group.setData(len(members), COUNT_ROLE)
            self._model.appendRow(group)
            for entry in members:
                item = QStandardItem(entry.title)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                item.setData(entry, ENTRY_ROLE)
                item.setData(_date(entry.updated_at), DATE_ROLE)
                item.setToolTip(entry.title)
                group.appendRow(item)
                if selected is not None and selected.conversation_id == entry.conversation_id:
                    self._list.setCurrentIndex(item.index())
        self._list.expandAll()
        self._list.verticalScrollBar().setValue(scroll)
        self._empty.setVisible(not entries)
        self._empty.setText("没有匹配的归档聊天。" if query or kind != "all" or project_id != "all" else "尚无已归档聊天。")

    def _set_state(self, state) -> None:
        self._list.setEnabled(state.ready and not state.busy and not state.pending_save)
        self._status.setText(state.error or ("正在读取会话目录…" if state.busy else ""))
        self._retry.setVisible(bool(state.error))
        self._retry.setText("重试保存目录" if state.pending_save else "刷新目录")
        self._retry.setEnabled(not state.busy)

    def _open(self, index) -> None:
        entry = index.data(ENTRY_ROLE)
        if entry is not None:
            self.conversation_requested.emit(entry.conversation_id)

    def _action(self, action: str, conversation_id: str) -> None:
        (self._catalog.restore if action == "restore" else self._catalog.delete_archived)(conversation_id)

    def _selected_action(self, operation) -> None:
        entry = self._list.currentIndex().data(ENTRY_ROLE)
        if entry is not None:
            operation(entry.conversation_id)
