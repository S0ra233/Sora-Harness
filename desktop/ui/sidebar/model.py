"""目录摘要到 Qt 行的适配；不读取日志、不调用模型、不保存元数据。"""
from dataclasses import dataclass, field

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt

from ...application.catalog import CatalogSnapshot, ConversationEntry, ProjectEntry

KIND_ROLE = int(Qt.ItemDataRole.UserRole) + 1
ID_ROLE = KIND_ROLE + 1
BUSY_ROLE = KIND_ROLE + 2
PIN_ROLE = KIND_ROLE + 3
ACTIVE_ROLE = KIND_ROLE + 4


@dataclass
class _Row:
    kind: str
    key: str
    value: str | ConversationEntry | ProjectEntry
    parent: "_Row | None" = None
    children: list["_Row"] = field(default_factory=list)
    position: int = 0

    def append(self, row: "_Row") -> None:
        row.parent, row.position = self, len(self.children)
        self.children.append(row)


def _rows(snapshot: CatalogSnapshot, query: str) -> _Row:
    root = _Row("root", "root", "")
    for key, title in (("pinned", "置顶"), ("projects", "项目"), ("chats", "聊天")):
        root.append(_Row("section", key, title))
    pinned, projects, chats = root.children
    matches = [item for item in snapshot.conversations if not item.archived and not item.deleted and query in item.title.casefold()]
    groups: dict[str | None, list[ConversationEntry]] = {}
    for entry in matches:
        groups.setdefault(entry.project_id, []).append(entry)
        if entry.pinned:
            pinned.append(_Row("conversation", f"pinned:{entry.conversation_id}", entry))
    for project in snapshot.projects:
        entries = groups.get(project.project_id, [])
        if query and not entries:
            continue
        group = _Row("project", project.project_id, project)
        projects.append(group)
        for entry in entries:
            group.append(_Row("conversation", f"project:{entry.conversation_id}", entry))
    for entry in groups.get(None, []):
        chats.append(_Row("conversation", f"chat:{entry.conversation_id}", entry))
    return root


class SidebarModel(QAbstractItemModel):
    """稳定 ID 对应多个显示行；运行变化只通知相关行。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._snapshot = CatalogSnapshot()
        self._query = ""
        self._root = _rows(self._snapshot, self._query)
        self._by_conversation: dict[str, list[_Row]] = {}
        self.running: set[str] = set()
        self.active_id: str | None = None

    def index(self, row: int, column: int, parent=QModelIndex()) -> QModelIndex:
        owner = parent.internalPointer() if parent.isValid() else self._root
        if column != 0 or row < 0 or row >= len(owner.children):
            return QModelIndex()
        return self.createIndex(row, column, owner.children[row])

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        owner = index.internalPointer().parent
        return QModelIndex() if owner is self._root else self.createIndex(owner.position, 0, owner)

    def rowCount(self, parent=QModelIndex()) -> int:
        if parent.isValid() and parent.column() != 0:
            return 0
        return len((parent.internalPointer() if parent.isValid() else self._root).children)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 1

    def data(self, index: QModelIndex, role=int(Qt.ItemDataRole.DisplayRole)):
        if not index.isValid():
            return None
        row = index.internalPointer()
        entry = row.value
        if role == int(Qt.ItemDataRole.DisplayRole):
            return entry.title if row.kind == "conversation" else entry.name if row.kind == "project" else entry
        if role == KIND_ROLE:
            return row.kind
        if role == ID_ROLE:
            return entry.conversation_id if row.kind == "conversation" else row.key
        if role == BUSY_ROLE:
            return row.kind == "conversation" and entry.conversation_id in self.running
        if role == PIN_ROLE:
            return row.kind == "conversation" and entry.pinned
        if role == ACTIVE_ROLE:
            return row.kind == "conversation" and entry.conversation_id == self.active_id
        if role == int(Qt.ItemDataRole.ToolTipRole):
            return entry.workspace if row.kind == "project" else entry.title if row.kind == "conversation" else None
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                if index.data(KIND_ROLE) == "conversation" else Qt.ItemFlag.ItemIsEnabled)

    def _index_for(self, row: _Row) -> QModelIndex:
        return self.createIndex(row.position, 0, row)

    def conversation_indexes(self, conversation_id: str) -> tuple[QModelIndex, ...]:
        return tuple(self._index_for(row) for row in self._by_conversation.get(conversation_id, ()))

    def _notify(self, conversation_id: str, roles: list[int]) -> None:
        for index in self.conversation_indexes(conversation_id):
            self.dataChanged.emit(index, index, roles)

    def set_running(self, conversation_id: str, busy: bool) -> None:
        if (conversation_id in self.running) == busy:
            return
        if busy:
            self.running.add(conversation_id)
        else:
            self.running.discard(conversation_id)
        self._notify(conversation_id, [BUSY_ROLE])

    def set_active(self, conversation_id: str | None) -> None:
        if self.active_id == conversation_id:
            return
        old, self.active_id = self.active_id, conversation_id
        for value in {old, conversation_id} - {None}:
            self._notify(value, [ACTIVE_ROLE])

    def set_snapshot(self, snapshot: CatalogSnapshot, query: str | None = None) -> bool:
        """只有分组、排序或搜索改变行结构时重置；改名与状态不重建整表。"""
        self._snapshot = snapshot
        if query is not None:
            self._query = query.strip().casefold()
        new_root = _rows(snapshot, self._query)

        def same_shape(left: _Row, right: _Row) -> bool:
            return left.key == right.key and len(left.children) == len(right.children) and all(
                same_shape(a, b) for a, b in zip(left.children, right.children)
            )

        if same_shape(self._root, new_root):
            def update(left: _Row, right: _Row) -> None:
                if left.value != right.value:
                    left.value = right.value
                    index = self._index_for(left)
                    self.dataChanged.emit(index, index, [])
                for a, b in zip(left.children, right.children):
                    update(a, b)
            update(self._root, new_root)
            return False
        self.beginResetModel()
        self._root = new_root
        self._by_conversation = {}
        def remember(row: _Row) -> None:
            if row.kind == "conversation":
                self._by_conversation.setdefault(row.value.conversation_id, []).append(row)
            for child in row.children:
                remember(child)
        remember(new_root)
        self.endResetModel()
        return True
