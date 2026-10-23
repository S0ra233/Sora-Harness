"""独立接口表单；普通字段生成草稿，能力映射由应用服务解析。"""
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFormLayout, QFrame, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)
from PySide6.QtGui import QColor, QPainter
from ..common.scrolling import ScrollMotion
from ..common.switch import SwitchToggle

from ...configuration.models import APIConnection, ConnectionDraft, ModelDefinition
from ...configuration.profiles import AdapterCatalog, resolve_endpoint_url
from ...llm.rules import API_STYLES
from ...llm.model_discovery import ModelListSource, RemoteModel
from ..common.disclosure import AnimatedDisclosure
from ..common.icons import icon
from ..common.widgets import ClickComboBox
from ..common.buttons import MotionButton
from .remote_models_dialog import RemoteModelsDialog
from ..shell import theme


class _ModelDelegate(QStyledItemDelegate):
    """模型行保留灰底，不绘制原生焦点竖线；下拉编辑器采用共用外壳。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.remote_models: tuple[RemoteModel, ...] = ()

    def initStyleOption(self, option, index) -> None:
        super().initStyleOption(option, index)
        option.state &= ~QStyle.StateFlag.State_HasFocus

    def paint(self, painter, option, index) -> None:
        prepared = QStyleOptionViewItem(option)
        self.initStyleOption(prepared, index)
        if prepared.state & QStyle.StateFlag.State_Selected:
            table = self.parent()
            first = table.visualRect(index.model().index(index.row(), 0))
            last = table.visualRect(index.model().index(index.row(), table.columnCount() - 1))
            row = first.united(last).adjusted(0, 2, 0, -2)
            painter.save()
            painter.setClipRect(option.rect)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.SELECTED))
            painter.drawRoundedRect(row, 8, 8)
            painter.restore()
            prepared.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, prepared, index)

    def createEditor(self, parent, option, index):
        if index.column() == 0:
            editor = ClickComboBox(parent, compact=True)
            editor.setEditable(True)
            editor.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            for model in self.remote_models:
                editor.addItem(model.model)
            return editor
        editor = super().createEditor(parent, option, index)
        if isinstance(editor, QLineEdit):
            editor.setFrame(False)
            # 编辑器盖住单元格原文字；透明背景会同时显示旧文字和正在输入的文字。
            background = theme.SELECTED if option.state & QStyle.StateFlag.State_Selected else theme.BACKGROUND
            editor.setStyleSheet(f"background: {background}; color: {theme.TEXT}; border: none; padding: 0 6px;")
        return editor

    def setEditorData(self, editor, index) -> None:
        if isinstance(editor, QComboBox):
            editor.setEditText(str(index.data(Qt.ItemDataRole.EditRole) or ""))
        else:
            super().setEditorData(editor, index)

    def setModelData(self, editor, model, index) -> None:
        if isinstance(editor, QComboBox):
            model.setData(index, editor.currentText(), Qt.ItemDataRole.EditRole)
        else:
            super().setModelData(editor, model, index)


class ConnectionEditor(QWidget):
    save_requested = Signal()
    cancel_requested = Signal()
    discover_requested = Signal()

    def __init__(self, adapters: AdapterCatalog, parent=None) -> None:
        super().__init__(parent)
        self._adapters = adapters
        self._original: ConnectionDraft | None = None
        self._capability_origin: tuple[str, str] | None = None
        self._definitions: dict[str, ModelDefinition] = {}
        self._context_edits: dict[str, str] = {}
        self._image_edits: dict[str, object] = {}
        self._capability_id: str | None = None
        self._filling = False
        self._remote_models: tuple[RemoteModel, ...] = ()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        self._heading = QLabel("新建 API 接口")
        self._heading.setObjectName("formSection")
        layout.addWidget(self._heading)
        card = QFrame()
        card.setObjectName("modelCard")
        body = QVBoxLayout(card)
        body.setContentsMargins(22, 22, 22, 22)
        body.setSpacing(18)
        connection_card = QFrame()
        connection_card.setObjectName("settingsCard")
        card_layout = QVBoxLayout(connection_card)
        card_layout.setContentsMargins(20, 16, 20, 16)
        form = QFormLayout()
        form.setSpacing(14)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._name = QLineEdit()
        self._name.setPlaceholderText("例如：日常聊天、翻译接口")
        self._url = QLineEdit()
        self._key = QLineEdit()
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setPlaceholderText("填写密钥")
        self._provider = ClickComboBox()
        for kind, profile in adapters.providers.items():
            self._provider.addItem(profile.label, kind)
        self._style = ClickComboBox()
        self._complete_url = SwitchToggle("使用完整请求地址")
        self._url_note = QLabel()
        self._url_note.setObjectName("settingsNote")
        self._url_note.setWordWrap(True)
        self._url_note.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow("名字", self._name)
        form.addRow("提供商", self._provider)
        form.addRow("API 协议", self._style)
        form.addRow("API URL", self._url)
        form.addRow("使用完整请求地址", self._complete_url)
        form.addRow("", self._url_note)
        form.addRow("密钥", self._key)
        card_layout.addLayout(form)
        body.addWidget(connection_card)
        self._key_note = QLabel("保存后记住密钥，重启可继续使用。")
        self._key_note.setObjectName("settingsNote")
        self._key_note.setWordWrap(True)
        body.addWidget(self._key_note)
        models_title = QLabel("模型列表")
        models_title.setObjectName("formSection")
        models_heading = QHBoxLayout()
        models_heading.addWidget(models_title, 1)
        self._discover = MotionButton("获取可用模型", variant="secondary")
        self._discover.clicked.connect(lambda checked=False: self.discover_requested.emit())
        models_heading.addWidget(self._discover)
        self._add_remote = MotionButton("选择加入列表", variant="secondary")
        self._add_remote.setEnabled(False)
        self._add_remote.clicked.connect(self._choose_remote)
        models_heading.addWidget(self._add_remote)
        body.addLayout(models_heading)
        self._table = QTableWidget(0, 2)
        self._table.motion = ScrollMotion(self._table)
        self._table.setObjectName("connectionModels")
        self._table.setHorizontalHeaderLabels(["API 模型 ID", "显示名称（可选）"])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().hide()
        self._table.verticalHeader().setDefaultSectionSize(36)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setShowGrid(False)
        self._model_delegate = _ModelDelegate(self._table)
        self._table.setItemDelegate(self._model_delegate)
        self._table.setMinimumHeight(126)
        self._table.setMaximumHeight(240)
        self._table.setWordWrap(False)
        model_surface = QFrame()
        model_surface.setObjectName("modelListSurface")
        surface_layout = QVBoxLayout(model_surface)
        surface_layout.setContentsMargins(8, 8, 8, 8)
        surface_layout.addWidget(self._table)
        self._table.setFrameShape(QFrame.Shape.NoFrame)
        self._table.viewport().setAutoFillBackground(False)
        body.addWidget(model_surface)
        model_buttons = QHBoxLayout()
        add = MotionButton("＋ 添加模型", variant="secondary")
        remove = MotionButton("移除所选模型", variant="secondary")
        model_buttons.addWidget(add)
        model_buttons.addWidget(remove)
        model_buttons.addStretch()
        body.addLayout(model_buttons)
        default_form = QFormLayout()
        default_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._default = ClickComboBox()
        self._default.setPlaceholderText("请先在上方添加模型")
        default_form.addRow("默认模型", self._default)
        body.addLayout(default_form)
        self._advanced_toggle = MotionButton(variant="ghost")
        self._advanced_toggle.setObjectName("advancedToggle")
        self._advanced_toggle.setText("能力与高级参数")
        self._advanced_toggle.setIcon(icon("right"))
        self._advanced_toggle.setCheckable(True)
        body.addWidget(self._advanced_toggle)
        advanced = QFrame()
        advanced.setObjectName("settingsCard")
        advanced_layout = QVBoxLayout(advanced)
        advanced_layout.setContentsMargins(20, 16, 20, 16)
        advanced_layout.setSpacing(12)
        self._capability_caption = QLabel()
        self._capability_caption.setTextFormat(Qt.TextFormat.PlainText)
        self._capability_caption.setObjectName("settingsNote")
        advanced_layout.addWidget(self._capability_caption)
        capabilities = QFormLayout()
        capabilities.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self._context = QLineEdit()
        self._context.setPlaceholderText("256000")
        self._image = ClickComboBox()
        for label, value in (("采用预设", "auto"), ("仅文本", False), ("图片与文本", True), ("未知", None)):
            self._image.addItem(label, value)
        capabilities.addRow("上下文容量", self._context)
        capabilities.addRow("输入能力", self._image)
        advanced_layout.addLayout(capabilities)
        note = QLabel("在列表中选择模型后编辑它的能力。上下文留空采用预设，未知模型默认 256k。")
        note.setObjectName("settingsNote")
        note.setWordWrap(True)
        advanced_layout.addWidget(note)
        reset = MotionButton("所选模型采用默认能力", variant="secondary")
        advanced_layout.addWidget(reset)
        self._advanced = AnimatedDisclosure(advanced)
        body.addWidget(self._advanced)
        self._status = QLabel()
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setWordWrap(True)
        self._status.setObjectName("settingsNote")
        body.addWidget(self._status)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = MotionButton("返回", variant="ghost")
        cancel.setObjectName("cancelButton")
        self._save = MotionButton("保存", variant="primary")
        self._save.setObjectName("primaryButton")
        buttons.addWidget(cancel)
        buttons.addWidget(self._save)
        body.addLayout(buttons)
        layout.addWidget(card)
        self._provider.currentIndexChanged.connect(self._provider_changed)
        self._style.currentIndexChanged.connect(self._show_url_note)
        self._style.currentIndexChanged.connect(self._selected_model_changed)
        self._complete_url.toggled.connect(self._show_url_note)
        self._url.textChanged.connect(self._show_url_note)
        self._url.textChanged.connect(self._clear_remote_models)
        self._provider.currentIndexChanged.connect(self._clear_remote_models)
        self._style.currentIndexChanged.connect(self._clear_remote_models)
        self._complete_url.toggled.connect(self._clear_remote_models)
        self._key.textEdited.connect(self._clear_remote_models)
        self._table.itemChanged.connect(self._models_changed)
        self._table.currentCellChanged.connect(self._selected_model_changed)
        self._context.textEdited.connect(self._edit_context)
        self._image.activated.connect(self._edit_image)
        add.clicked.connect(self._add_model)
        remove.clicked.connect(self._remove_model)
        reset.clicked.connect(self._reset_capabilities)
        self._advanced_toggle.toggled.connect(self._toggle_advanced)
        cancel.clicked.connect(lambda checked=False: self.cancel_requested.emit())
        self._save.clicked.connect(lambda checked=False: self.save_requested.emit())
        self._provider_changed()

    def load(self, draft: ConnectionDraft | None) -> None:
        if draft is not None and draft.connection.provider_kind not in self._adapters.providers:
            raise ValueError(f"提供商 {draft.connection.provider_kind}已不在适配目录中，请修正适配资料后编辑。")
        self._filling = True
        self._original = draft
        self._capability_origin = (draft.connection.provider_kind, draft.connection.api_style) if draft else None
        self._definitions.clear()
        self._remote_models = ()
        self._model_delegate.remote_models = ()
        self._add_remote.setEnabled(False)
        self._context_edits.clear()
        self._image_edits.clear()
        self._table.setRowCount(0)
        self._default.clear()
        self._key.clear()
        self._status.clear()
        self._heading.setText("编辑 API 接口" if draft else "新建 API 接口")
        self._save.setText("保存" if draft else "创建接口")
        self._key.setPlaceholderText("已保存；留空保留密钥" if draft and draft.connection.credential_ref else "填写密钥")
        self._key_note.setText("密钥保存在此 Windows 用户的系统存储中。" if draft and draft.connection.credential_ref
                               else "原接口使用环境变量，留空仍沿用；填写后改用记住的密钥。" if draft
                               else "保存后记住密钥，重启可继续使用。")
        connection = draft.connection if draft else None
        self._name.setText(connection.label if connection else "")
        self._url.setText(connection.api_url if connection else "")
        self._complete_url.setChecked(connection is not None and connection.url_mode == "endpoint")
        self._provider.setCurrentIndex(max(0, self._provider.findData(connection.provider_kind if connection else "compatible")))
        self._provider_changed()
        if connection is not None:
            style_index = self._style.findData(connection.api_style)
            if style_index < 0:
                self._style.addItem(API_STYLES.get(connection.api_style, connection.api_style), connection.api_style)
                style_index = self._style.count() - 1
            self._style.setCurrentIndex(style_index)
            for key, definition in connection.models.items():
                self._definitions[key] = deepcopy(definition)
                self._append_row(key, definition.model, definition.label)
        else:
            self._append_row(uuid4().hex, "", "")
        self._filling = False
        self._models_changed()
        if connection is not None:
            self._default.setCurrentIndex(self._default.findData(connection.default_model))
        self._table.setCurrentCell(0, 0)
        self._show_capabilities()
        self._advanced_toggle.setChecked(False)
        self._advanced.set_expanded(False, animate=False)
        self._show_url_note()

    def _append_row(self, identifier: str, model: str, label: str) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        item = QTableWidgetItem(model)
        item.setData(Qt.ItemDataRole.UserRole, identifier)
        self._table.setItem(row, 0, item)
        self._table.setItem(row, 1, QTableWidgetItem(label))

    def _add_model(self) -> None:
        self._filling = True
        self._append_row(uuid4().hex, "", "")
        self._filling = False
        self._models_changed()
        self._table.setCurrentCell(self._table.rowCount() - 1, 0)
        self._table.editItem(self._table.currentItem())

    def _remove_model(self) -> None:
        row = self._table.currentRow()
        if row >= 0:
            self._table.removeRow(row)
            self._models_changed()
            self._show_capabilities()

    def _models_changed(self, *args) -> None:
        if self._filling:
            return
        selected = self._default.currentData()
        self._default.blockSignals(True)
        self._default.clear()
        for row in range(self._table.rowCount()):
            item, label = self._table.item(row, 0), self._table.item(row, 1)
            if item.text().strip():
                name = item.text().strip()
                self._default.addItem(name, item.data(Qt.ItemDataRole.UserRole))
        index = self._default.findData(selected) if selected is not None else -1
        self._default.setCurrentIndex(max(0, index) if self._default.count() else -1)
        self._default.setEnabled(self._default.count() > 0)
        self._default.blockSignals(False)
        self._show_capabilities()

    def _provider_changed(self, *args) -> None:
        previous = self._style.currentData()
        self._style.blockSignals(True)
        self._style.clear()
        profile = self._adapters.providers[self._provider.currentData()]
        for style in profile.protocols:
            self._style.addItem(API_STYLES[style], style)
        self._style.setCurrentIndex(max(0, self._style.findData(previous)))
        self._style.blockSignals(False)
        self._show_url_note()
        if not self._filling:
            self._show_capabilities()

    def _show_url_note(self, *args) -> None:
        profile = self._adapters.providers[self._provider.currentData()]
        protocol = profile.protocols.get(self._style.currentData())
        if protocol is not None:
            example = protocol.endpoint_example
            if not self._complete_url.isChecked():
                for suffix in ("/chat/completions", "/responses", "/messages", "/models/{model}:generateContent"):
                    if example.endswith(suffix):
                        example = example[:-len(suffix)]
                        break
            self._url.setPlaceholderText(example)
        note = "填写完整请求 URL。" if self._complete_url.isChecked() else "填写 API 基础地址，应用按所选协议补全请求路径。"
        if self._url.text().strip():
            try:
                target = resolve_endpoint_url(self._url.text(), self._style.currentData(),
                                              "endpoint" if self._complete_url.isChecked() else "base")
                note = f"请求地址：{target}"
            except ValueError:
                note = "请填写有效的 API URL。"
        self._url_note.setText(note)

    def _selected_model_changed(self, *args) -> None:
        if not self._filling:
            self._show_capabilities()

    def _show_capabilities(self) -> None:
        row = self._table.currentRow()
        item = self._table.item(row, 0) if row >= 0 else None
        self._capability_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        self._context.setEnabled(item is not None)
        self._image.setEnabled(item is not None)
        if item is None:
            self._capability_caption.setText("请选择一个模型。")
            return
        key, name = self._capability_id, item.text().strip()
        original = self._definitions.get(key)
        overrides = original.overrides if original is not None and original.model == name else {}
        try:
            preset = self._adapters.capabilities(self._provider.currentData(), self._style.currentData(), name)
        except ValueError as exc:
            self._status.setText(str(exc))
            preset = self._adapters.defaults
        value = overrides.get("context_window")
        self._context.setText(self._context_edits.get(key, str(value) if value is not None else ""))
        self._context.setPlaceholderText(str(preset.get("context_window") or 256000))
        image = self._image_edits.get(key, overrides.get("image_input", "auto"))
        # 按类型逐项比较，明确区分false、null和“采用预设”的字符串。
        index = next(index for index in range(self._image.count())
                     if type(self._image.itemData(index)) is type(image) and self._image.itemData(index) == image)
        self._image.setCurrentIndex(index)
        capability = {True: "图片与文本", False: "仅文本", None: "输入能力未确认"}[preset.get("image_input")]
        self._capability_caption.setText(f"所选模型：{name or '未填写'} · 预设：{capability}")

    def _edit_context(self, text: str) -> None:
        if self._capability_id is not None:
            self._context_edits[self._capability_id] = text

    def _edit_image(self, index: int) -> None:
        if self._capability_id is not None:
            self._image_edits[self._capability_id] = self._image.itemData(index)

    def _reset_capabilities(self) -> None:
        key = self._capability_id
        if key is None:
            return
        if key in self._definitions:
            self._definitions[key] = replace(self._definitions[key], overrides={})
        self._context_edits.pop(key, None)
        self._image_edits.pop(key, None)
        self._show_capabilities()

    def _toggle_advanced(self, expanded: bool) -> None:
        self._advanced_toggle.setIcon(icon("chevron" if expanded else "right"))
        self._advanced.set_expanded(expanded)

    def read_draft(self) -> ConnectionDraft:
        """控件只表达普通输入和覆盖意图；隐藏的旧能力未修改时保留。"""
        models = {}
        for row in range(self._table.rowCount()):
            item, label = self._table.item(row, 0), self._table.item(row, 1)
            identifier, name = item.data(Qt.ItemDataRole.UserRole), item.text().strip()
            if not name and not label.text().strip():
                continue
            original = self._definitions.get(identifier)
            overrides = deepcopy(original.overrides) if original is not None and original.model == name else {}
            if identifier in self._context_edits:
                text = self._context_edits[identifier].strip()
                overrides.pop("context_window", None)
                if text:
                    try:
                        overrides["context_window"] = int(text)
                    except ValueError as exc:
                        raise ValueError(f"模型 {name}的上下文容量须为正整数。") from exc
            if identifier in self._image_edits:
                value = self._image_edits[identifier]
                overrides.pop("image_input", None)
                if value != "auto":
                    overrides["image_input"] = value
            if self._capability_origin is not None and self._capability_origin != (self._provider.currentData(), self._style.currentData()):
                overrides.pop("reasoning", None)
            models[identifier] = ModelDefinition(name, label.text().strip() or name, overrides)
        default_id = self._default.currentData()
        if default_id not in models:
            raise ValueError("请先添加模型，并从上方模型列表选择默认模型。")
        previous = self._original.connection if self._original else None
        connection = APIConnection(
            label=self._name.text().strip(), api_url=self._url.text().strip(),
            url_mode="endpoint" if self._complete_url.isChecked() else "base",
            provider_kind=self._provider.currentData(), api_style=self._style.currentData(),
            models=models, default_model=default_id,
            credential_ref=previous.credential_ref if previous else None,
            key_env=previous.key_env if previous else "MODEL_API_KEY",
            timeout_seconds=previous.timeout_seconds if previous else 180.0,
            connect_timeout_seconds=previous.connect_timeout_seconds if previous else 30.0,
            max_attempts=previous.max_attempts if previous else 5,
        )
        return ConnectionDraft(connection, self._original.connection_id if self._original else None)

    def key_update(self) -> str:
        return self._key.text()

    def form_extra(self):
        return {"default": self._default.currentData(), "rows": [[self._table.item(row, 0).data(Qt.ItemDataRole.UserRole), self._table.item(row, 0).text(),
                           self._table.item(row, 1).text()] for row in range(self._table.rowCount())],
                "context": dict(self._context_edits), "image": dict(self._image_edits)}

    def restore_form_extra(self, data):
        self._filling = True
        try:
            self._table.setRowCount(0)
            for identifier, name, label in data["rows"]:
                self._append_row(identifier, name, label)
            self._context_edits = dict(data["context"])
            self._image_edits = dict(data["image"])
        finally:
            self._filling = False
        self._models_changed()
        self._default.setCurrentIndex(self._default.findData(data.get("default")))

    def discovery_source(self) -> ModelListSource:
        """查询不要求先填模型；地址变化后不得沿用原接口的凭据。"""
        previous = self._original.connection if self._original else None
        source = ModelListSource(self._url.text().strip(), self._style.currentData(),
                                 "endpoint" if self._complete_url.isChecked() else "base")
        if previous is not None:
            same = (previous.provider_kind == self._provider.currentData() and previous.api_style == source.api_style
                    and resolve_endpoint_url(previous.api_url, previous.api_style, previous.url_mode)
                    == resolve_endpoint_url(source.api_url, source.api_style, source.url_mode))
            if same:
                return replace(source, credential_ref=previous.credential_ref, key_env=previous.key_env)
            if not self.key_update().strip():
                raise ValueError("地址、提供商或协议已改变，请填写该接口的密钥后获取模型。")
        return source

    def add_remote_models(self, models: tuple[RemoteModel, ...]) -> None:
        existing = {self._table.item(row, 0).text().strip() for row in range(self._table.rowCount())}
        self._filling = True
        for model in models:
            if model.model in existing:
                continue
            blank = next((row for row in range(self._table.rowCount()) if not self._table.item(row, 0).text().strip()
                          and not self._table.item(row, 1).text().strip()), None)
            if blank is None:
                self._append_row(uuid4().hex, model.model, model.label)
            else:
                self._table.item(blank, 0).setText(model.model)
                self._table.item(blank, 1).setText(model.label)
            existing.add(model.model)
        self._filling = False
        self._models_changed()

    def set_remote_models(self, models: tuple[RemoteModel, ...]) -> None:
        """只更新本次查询候选，既有表格和手填默认值不自动改变。"""
        self._remote_models = models
        self._model_delegate.remote_models = models
        self._add_remote.setEnabled(bool(models))
        self._models_changed()

    def _clear_remote_models(self, *args) -> None:
        if self._remote_models:
            self.set_remote_models(())

    def _choose_remote(self) -> None:
        dialog = RemoteModelsDialog(self._remote_models, self)
        if dialog.exec() == dialog.DialogCode.Accepted:
            self.add_remote_models(dialog.selection())
            self.set_status("已加入表单，点击保存后才写入接口配置。")
        dialog.deleteLater()

    def set_discovering(self, busy: bool) -> None:
        self.setEnabled(not busy)
        self._discover.setText("正在获取…" if busy else "获取可用模型")

    def set_retry(self, draft: ConnectionDraft) -> None:
        """只更新身份／密钥引用，不回填表单，保留用户输入与能力草稿。"""
        self._original = draft

    def clear_key(self) -> None:
        self._key.clear()

    def set_status(self, text: str) -> None:
        self._status.setText(text)

