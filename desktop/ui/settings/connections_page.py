"""接口列表只展示非敏感目录并发出用户意图，不读取密钥。"""
from PySide6.QtCore import Qt, Signal
from ..common.buttons import MotionButton
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ...configuration.models import ConnectionCatalog
from ..common.widgets import ElidedLabel


class ConnectionsPage(QWidget):
    create_requested = Signal()
    edit_requested = Signal(str)
    activate_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        row = QHBoxLayout()
        heading = QLabel("API 接口")
        heading.setObjectName("formSection")
        create = MotionButton("＋ 新建 API 接口", variant="primary")
        self._create = create
        create.setObjectName("primaryButton")
        create.clicked.connect(lambda checked=False: self.create_requested.emit())
        row.addWidget(heading)
        row.addStretch()
        row.addWidget(create)
        layout.addLayout(row)
        self._cards = QWidget()
        self._cards_layout = QVBoxLayout(self._cards)
        self._cards_layout.setContentsMargins(0, 0, 0, 0)
        self._cards_layout.setSpacing(12)
        layout.addWidget(self._cards)
        self._empty = QLabel("还没有 API 接口。\n新建接口后，可以添加多个模型供聊天选择。")
        self._empty.setObjectName("settingsNote")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        self._empty.setMinimumHeight(190)
        layout.addWidget(self._empty)
        self._status = QLabel()
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setWordWrap(True)
        self._status.setObjectName("settingsNote")
        layout.addWidget(self._status)
        layout.addStretch()

    def set_catalog(self, catalog: ConnectionCatalog) -> None:
        while self._cards_layout.count():
            widget = self._cards_layout.takeAt(0).widget()
            widget.hide()
            widget.deleteLater()
        self._empty.setVisible(not catalog.connections)
        for identifier, connection in catalog.connections.items():
            card = QFrame()
            card.setObjectName("connectionCard")
            card.setProperty("active", identifier == catalog.active_connection)
            row = QHBoxLayout(card)
            row.setContentsMargins(20, 18, 18, 18)
            row.setSpacing(14)
            texts = QVBoxLayout()
            texts.setSpacing(6)
            label = ElidedLabel(connection.label)
            label.setObjectName("formSection")
            label.setToolTip(connection.label)
            endpoint = ElidedLabel(connection.api_url)
            endpoint.setObjectName("settingsNote")
            endpoint.setToolTip(connection.api_url)
            default = connection.models[connection.default_model]
            summary = ElidedLabel(f"{len(connection.models)} 个模型 · 默认 {default.label}")
            summary.setObjectName("settingsNote")
            texts.addWidget(label)
            texts.addWidget(endpoint)
            texts.addWidget(summary)
            row.addLayout(texts, 1)
            active = identifier == catalog.active_connection
            activate = MotionButton("已启用" if active else "启用", variant="secondary")
            activate.setEnabled(not active)
            activate.clicked.connect(lambda checked=False, key=identifier: self.activate_requested.emit(key))
            edit = MotionButton("编辑", variant="secondary")
            edit.clicked.connect(lambda checked=False, key=identifier: self.edit_requested.emit(key))
            row.addWidget(activate)
            row.addWidget(edit)
            self._cards_layout.addWidget(card)

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def set_editing_enabled(self, enabled: bool) -> None:
        self._create.setEnabled(enabled)
        self._cards.setEnabled(enabled)
