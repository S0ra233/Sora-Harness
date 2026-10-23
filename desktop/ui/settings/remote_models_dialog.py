"""远端模型目录只供选择，不自动覆盖已保存模型和能力。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialogButtonBox, QLabel, QLineEdit, QListWidget, QListWidgetItem

from ...llm.model_discovery import RemoteModel
from ..common.dialogs import SurfaceDialog
from ..common.scrolling import ScrollMotion


class RemoteModelsDialog(SurfaceDialog):
    def __init__(self, models: tuple[RemoteModel, ...], parent=None) -> None:
        super().__init__("选择要添加的模型", parent, width=520)
        note = QLabel("模型目录不代表图片或思考能力，能力仍按适配资料与显式配置确定。")
        note.setWordWrap(True)
        self.body.addWidget(note)
        search = QLineEdit()
        search.setPlaceholderText("搜索模型")
        self.body.addWidget(search)
        self._list = QListWidget()
        self._list.motion = ScrollMotion(self._list)
        self._list.setMinimumHeight(240)
        self._list.setStyleSheet("QListWidget { background: white; border: none; outline: none; }"
                                 "QListWidget::item { color: #24282d; padding: 9px; border: none; }"
                                 "QListWidget::item:selected { background: #edeff3; color: #24282d; }")
        for model in models:
            item = QListWidgetItem(model.model if model.label == model.model else f"{model.label} · {model.model}")
            item.setData(Qt.ItemDataRole.UserRole, model)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self._list.addItem(item)
        self.body.addWidget(self._list)
        confirm = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        confirm.setText("添加所选模型")
        confirm.setEnabled(False)
        self._list.itemChanged.connect(lambda _: confirm.setEnabled(bool(self.selection())))
        search.textChanged.connect(self._filter)
        self.buttons.accepted.connect(self.accept)

    def _filter(self, text: str) -> None:
        for row in range(self._list.count()):
            item = self._list.item(row)
            item.setHidden(text.casefold() not in item.text().casefold())

    def selection(self) -> tuple[RemoteModel, ...]:
        return tuple(self._list.item(row).data(Qt.ItemDataRole.UserRole) for row in range(self._list.count())
                     if self._list.item(row).checkState() == Qt.CheckState.Checked)
