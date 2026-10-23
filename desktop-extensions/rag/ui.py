"""设置贡献与辅助面板复用宿主控件；没有主模型请求入口。"""
import asyncio
import json
from desktop.storage.atomic import write_text_atomic
from desktop.plugin_api.core import document_readers

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                              QListWidget, QListWidgetItem, QPlainTextEdit, QSpinBox, QVBoxLayout, QWidget)

from desktop.plugin_api.ui import (AnimatedDisclosure, CardSurface, ClickComboBox, CloseBlock, FormAutoSave,
                                   MotionButton, SwitchToggle, UiContribution, UiPageDefinition, WhiteSurface, theme)
from .service import LibraryService
from .settings import EmbeddingOptions
from .worker import WorkOwner
from .projection import decorate


def label(text="", *, note=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    if note:
        widget.setObjectName("settingsNote")
    return widget


def buttons(layout, *widgets):
    row = QHBoxLayout()
    row.setSpacing(8)
    for widget in widgets:
        row.addWidget(widget)
    row.addStretch()
    layout.addLayout(row)


def apply_style(widget, name):
    """沿宿主配色绘制输入与结果，不使用系统蓝色选中条或强焦点框。"""
    widget.setObjectName(name)
    widget.setStyleSheet(f"""
        QWidget#{name} QLineEdit {{ background: {theme.CONTROL}; color: {theme.TEXT}; border: none;
            border-radius: 10px; padding: 9px 12px; selection-background-color: {theme.TEXT_SELECTION}; selection-color: {theme.TEXT}; }}
        QWidget#{name} QSpinBox {{ background: {theme.BACKGROUND}; color: {theme.TEXT}; border: 1px solid {theme.BORDER}; border-radius: 8px; padding: 6px; }}
        QWidget#{name} QPlainTextEdit {{ background: {theme.BACKGROUND}; color: {theme.TEXT}; border: none;
            selection-background-color: {theme.TEXT_SELECTION}; selection-color: {theme.TEXT}; }}
        QWidget#{name} QListWidget {{ background: {theme.BACKGROUND}; color: {theme.TEXT}; border: none; outline: none; }}
        QWidget#{name} QListWidget::item {{ padding: 10px; margin: 2px; border-radius: 10px; }}
        QWidget#{name} QListWidget::item:hover {{ background: {theme.HOVER}; color: {theme.TEXT}; }}
        QWidget#{name} QListWidget::item:selected {{ background: {theme.SELECTED}; color: {theme.TEXT}; }}
    """)


class LibrarySettings(WhiteSurface, WorkOwner):
    changed = Signal()

    def __init__(self, service, tasks):
        super().__init__()
        apply_style(self, "ragSettings")
        self.setWindowTitle("资料库配置")
        self.service = service
        self._init_jobs(tasks)
        self._loading, self._dirty, self._shown = True, False, False
        self._auto = None
        self._libraries = {}
        self._preferred_library = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(18)
        self.setMaximumWidth(940)
        outer.addWidget(label("检索工具默认随插件开放。资料仅来自下方启用的库；模型在工作会话调用，普通聊天可打开辅助页。", note=True))
        content = QWidget()
        body = QVBoxLayout(content)
        body.setSpacing(12)
        self.library = ClickComboBox()
        self.library.setAccessibleName("当前资料库")
        self.library.currentIndexChanged.connect(self._selected)
        body.addWidget(self.library)
        self.summary = label("尚未建立资料库", note=True)
        body.addWidget(self.summary)
        self.available = SwitchToggle("启用此资料库")
        self.available.toggled.connect(self._toggle_library)
        body.addWidget(self.available)
        self.name = QLineEdit()
        self.name.setPlaceholderText("新资料库名称，例如：世界设定、翻译语料")
        add = MotionButton("新建资料库", variant="secondary")
        add.clicked.connect(self._add)
        buttons(body, self.name, add)
        encoding_row = QFormLayout()
        self.encoding = ClickComboBox()
        for title, value in (("自动 · UTF-8／带BOM的UTF-16", "auto"), ("UTF-8", "utf-8-sig"),
                             ("UTF-16", "utf-16"), ("GB18030", "gb18030"), ("Shift-JIS", "shift_jis")):
            self.encoding.addItem(title, value)
        encoding_row.addRow("资料编码", self.encoding)
        body.addLayout(encoding_row)
        imported, rebuild, reload = MotionButton("导入资料…", variant="secondary"), MotionButton("建立／补齐向量", variant="secondary"), MotionButton("刷新资料库", variant="secondary")
        imported.clicked.connect(self._import)
        rebuild.clicked.connect(self._embed)
        reload.clicked.connect(self.reload_libraries)
        buttons(body, imported, rebuild, reload)
        body.addWidget(label("可一次选择多份文件。单份最多64MiB；自动编码不猜测旧中文／日文编码。更新同一路径只切换有效版本，旧原文仍保留。", note=True))
        outer.addWidget(CardSurface(content))
        connection = QWidget()
        form = QFormLayout(connection)
        form.setSpacing(14)
        self.enabled = SwitchToggle("启用语义接口")
        self.protocol = ClickComboBox()
        for title, value in (("OpenAI兼容 · Embeddings", "openai"), ("Ollama · Embed", "ollama"), ("Gemini · Embeddings", "gemini")):
            self.protocol.addItem(title, value)
        self.url, self.model, self.key = QLineEdit(), QLineEdit(), QLineEdit()
        self.url.setPlaceholderText("接口基础地址，包含版本路径，例如 https://接口/v1")
        self.model.setPlaceholderText("Embedding模型名称，与聊天模型单独配置")
        self.key.setPlaceholderText("密钥或 ${RAG_EMBEDDING_KEY}；本地服务可留空")
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        for title, widget in (("启用语义接口", self.enabled), ("接口协议", self.protocol), ("接口地址", self.url), ("模型", self.model), ("API Key", self.key)):
            form.addRow(title, widget)
        outer.addWidget(label("Embedding接口"))
        outer.addWidget(CardSurface(connection))
        advanced = QWidget()
        limits = QFormLayout(advanced)
        self.dimensions, self.timeout, self.batch = QSpinBox(), QSpinBox(), QSpinBox()
        self.dimensions.setRange(0, 16384)
        self.dimensions.setSpecialValueText("模型默认")
        self.timeout.setRange(1, 600)
        self.batch.setRange(1, 64)
        for title, widget in (("向量维度", self.dimensions), ("单次请求时限（秒）", self.timeout), ("批量片段数", self.batch)):
            limits.addRow(title, widget)
        advanced_card = CardSurface(advanced)
        toggle = MotionButton("高级参数", variant="ghost")
        toggle.setCheckable(True)
        disclosure = AnimatedDisclosure(advanced_card, self)
        toggle.toggled.connect(disclosure.set_expanded)
        outer.addWidget(toggle)
        outer.addWidget(disclosure)
        outer.addWidget(label("保存不会请求接口。主动建立向量会把所选资料发送到该Embedding服务；关键词检索不发送。地址、模型或维度改变后须补建对应索引。Gemini填至/v1beta，Ollama填服务根地址。", note=True))
        save, revert = MotionButton("保存接口", variant="primary"), MotionButton("重新读取接口", variant="secondary")
        save.clicked.connect(self._save)
        revert.clicked.connect(self.reload_options)
        buttons(outer, save, revert)
        self.status = label(note=True)
        self.stop = MotionButton("停止当前操作", variant="secondary")
        self.stop.setEnabled(False)
        self.stop.clicked.connect(self._stop)
        buttons(outer, self.stop)
        outer.addWidget(self.status)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setMaximumHeight(160)
        self.report.setPlaceholderText("导入与索引结果")
        outer.addWidget(self.report)
        reader_body = QWidget()
        reader_form = QVBoxLayout(reader_body)
        reader_form.addWidget(label("PDF／DOCX转换", note=False))
        reader_form.addWidget(label("选择已启用并同步的本地convert_to_markdown MCP；导入时才调用。原文件保留，转换行号不是PDF页码；扫描件效果由服务决定。", note=True))
        self.reader = ClickComboBox()
        reader_refresh = MotionButton("刷新转换服务", variant="ghost")
        buttons(reader_form, self.reader, reader_refresh)
        outer.addWidget(CardSurface(reader_body))
        self._reader_path = service.store.directory / "document-reader.json"
        self._reader_loading = False
        self._reader_dirty = False
        self._load_readers()
        reader_refresh.clicked.connect(self._load_readers)
        self.reader.currentIndexChanged.connect(self._save_reader)
        for widget in (self.url, self.model, self.key):
            widget.textChanged.connect(self._mark)
        self.enabled.toggled.connect(self._mark)
        self.protocol.currentIndexChanged.connect(self._mark)
        for widget in (self.dimensions, self.timeout, self.batch):
            widget.valueChanged.connect(self._mark)
        self.reload_options()
        self._auto = FormAutoSave(self, {"protocol": self.protocol, "url": self.url, "model": self.model,
            "api_key": self.key, "dimensions": self.dimensions, "timeout": self.timeout, "batch": self.batch,
            "enabled": self.enabled}, lambda: self._save(automatic=True), service.settings.path.with_name("embedding-draft.json"),
            secrets=("api_key",), dirty=lambda value: setattr(self, "_dirty", value), status=self.status,
            ready=lambda: not self._loading and self._worker is None)
        self._auto.reset()

    def _load_readers(self):
        self._reader_loading = True
        try:
            saved = json.loads(self._reader_path.read_text(encoding="utf-8")) if self._reader_path.exists() else None
            self.reader.clear()
            self.reader.addItem("未配置 · 仅导入TXT／Markdown", None)
            for server, tool in document_readers(self.service.store.directory.parent.parent):
                value = [server.server_id, tool["name"]]
                self.reader.addItem(server.name + " · " + tool["name"], value)
            self.reader.setCurrentIndex(max(0, self.reader.findData(saved)))
            self._reader_dirty = False
            if saved is not None and self.reader.currentIndex() == 0:
                self.status.setText("原文档转换服务不可用；原选择保留，重新启用或选择其他服务。")
        except (OSError, ValueError) as exc:
            self.status.setText(f"转换服务未读取：{exc}")
        finally:
            self._reader_loading = False

    def _save_reader(self, *_):
        if self._reader_loading:
            return
        try:
            write_text_atomic(self._reader_path, json.dumps(self.reader.currentData(), ensure_ascii=False))
            self._reader_dirty = False
        except (OSError, ValueError) as exc:
            self._reader_dirty = True
            self.status.setText(f"文档转换选择未保存：{exc}")

    def showEvent(self, event):
        super().showEvent(event)
        if not self._shown:
            self._shown = True
            self.reload_libraries()

    def close_block(self):
        return self._busy_block() or self._auto.close_block() or (CloseBlock("文档转换选择未保存", "请重新选择或刷新转换服务后再关闭。") if self._reader_dirty else None)

    def _mark(self, *_):
        if not self._loading:
            if self._auto is not None:
                self._auto.changed()

    def reload_options(self):
        if self._worker is not None:
            self.status.setText("请先等待当前操作结束。")
            return
        if self._auto is not None and not self._auto.flush():
            return
        self._loading = True
        try:
            config = self.service.settings.load()
            self.enabled.setChecked(config.enabled)
            self.protocol.setCurrentIndex(self.protocol.findData(config.protocol))
            for widget, value in ((self.url, config.url), (self.model, config.model), (self.key, config.api_key)):
                widget.setText(value)
            self.dimensions.setValue(config.dimensions or 0)
            self.timeout.setValue(config.timeout_seconds)
            self.batch.setValue(config.batch_size)
            self._dirty = False
        except (OSError, ValueError) as exc:
            self.status.setText(f"接口未读取：{exc}")
        finally:
            self._loading = False
        if self._auto is not None:
            self._auto.reset()

    def _save(self, _checked=False, *, automatic=False):
        if self._worker is not None:
            self.status.setText("请先等待当前操作结束再保存接口。")
            return False
        try:
            self.service.settings.save(EmbeddingOptions(self.protocol.currentData(), self.url.text().strip(),
                self.model.text().strip(), self.key.text().strip(), self.dimensions.value() or None,
                self.timeout.value(), self.batch.value(), self.enabled.isChecked()))
        except (OSError, ValueError) as exc:
            self.status.setText(f"接口未保存：{exc}")
            return False
        else:
            self._dirty = False
            self.status.setText("已保存接口；关键词工具声明保持不变。")
            self.changed.emit()
            if not automatic:
                self._auto.reset(restore=False)
                self._auto.flush()
                self.reload_libraries()
            return True

    def reload_libraries(self):
        selected = self._preferred_library or self.library.currentData()
        self._preferred_library = None
        async def operation(cancel, progress):
            warning = ""
            try:
                space = self.service.settings.load().fingerprint()
            except (OSError, ValueError):
                space = None
                warning = "Embedding设置未读取，关键词资料仍可管理。"
            data = await asyncio.to_thread(self.service.store.libraries, limit=1000, space=space)
            data["warning"] = warning
            return data
        def receive(data):
            self._loading = True
            try:
                self.library.clear()
                self._libraries = {item["id"]: item for item in data["libraries"]}
                for item in data["libraries"]:
                    self.library.addItem(item["name"], item["id"])
                index = self.library.findData(selected)
                if index >= 0:
                    self.library.setCurrentIndex(index)
                self._selected()
            finally:
                self._loading = False
            self.status.setText("资料库已刷新" + ("；仅显示前1000库。" if data["next_offset"] is not None else "。") + data["warning"])
        self._start(operation, receive)

    def _selected(self, *_):
        item = self._libraries.get(self.library.currentData())
        self.available.blockSignals(True)
        self.available.setEnabled(item is not None)
        self.available.setChecked(bool(item and item["enabled"]))
        self.available.blockSignals(False)
        self.summary.setText(f"{item['documents']} 份资料 · {item['chunks']} 个片段 · 当前模型向量 {item.get('embedded_chunks', '未知')} 个" if item else "尚未建立资料库")

    def _toggle_library(self, enabled):
        identifier = self.library.currentData()
        if self._loading or identifier is None:
            return
        async def operation(cancel, progress):
            await asyncio.to_thread(self.service.store.enable_library, identifier, enabled)
            return {}
        def receive(_):
            self.changed.emit()
            self.reload_libraries()
        if self._worker is not None:
            self._selected()
        else:
            self._start(operation, receive)

    def _add(self):
        name = self.name.text()
        async def operation(cancel, progress):
            return await asyncio.to_thread(self.service.store.create_library, name)
        def receive(identifier):
            self._preferred_library = identifier
            self.name.clear()
            self.changed.emit()
            self.reload_libraries()
        self._start(operation, receive)

    def _import(self):
        identifier = self.library.currentData()
        if not identifier:
            self.status.setText("请先创建资料库。")
            return
        if self._worker is not None:
            self.status.setText("当前操作尚未结束。")
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "导入资料", "", "资料文件 (*.txt *.md *.markdown *.pdf *.docx)")
        if not paths:
            return
        if len(paths) > 10000:
            self.status.setText("单批最多10000份资料，请分批导入。")
            return
        encoding = self.encoding.currentData()
        reader = self.reader.currentData()
        async def operation(cancel, progress):
            return await self.service.import_documents(identifier, paths, encoding, reader, cancel=cancel, progress=progress)
        self._start(operation, self._report)

    def _embed(self):
        if not self._auto.flush():
            return
        identifier = self.library.currentData()
        if not identifier or not self.available.isChecked():
            self.status.setText("请先选中并启用资料库。")
            return
        if self._dirty:
            self.status.setText("请先保存Embedding接口。")
            return
        async def operation(cancel, progress):
            return await self.service.build_embeddings(identifier, cancel=cancel, progress=progress)
        self._start(operation, self._report)

    def _report(self, result):
        if "imported" in result:
            lines = [f"已导入 {result['imported']} 份，未变化 {result['unchanged']} 份，失败 {result['failed']} 份。"]
            lines.extend(f"{item['file']}：{item['error']}" for item in result["errors"])
            if result["errors_truncated"]:
                lines.append("这里只显示前32项失败。")
        else:
            lines = [f"本次建立 {result['embedded_chunks']} 个片段的向量，请求 {result['requests']} 次。", result["note"]]
            names = {"prompt_tokens": "输入token", "input_tokens": "输入token", "total_tokens": "总token",
                     "promptTokenCount": "输入token", "totalTokenCount": "总token"}
            lines.extend(f"接口返回的{names[key]}：{value}" for key, value in result["usage"].items() if key in names)
        self.report.setPlainText("\n".join(lines))
        self.changed.emit()
        self.reload_libraries()

    def _job_failed(self):
        self._selected()
        self.changed.emit()


class LibraryView(WhiteSurface, WorkOwner):
    def __init__(self, service, tasks, context):
        super().__init__()
        apply_style(self, "ragView")
        self.service, self.context = service, context
        self._init_jobs(tasks)
        self._loaded = False
        self._chunk, self._offset, self._next, self._start_char = None, 0, None, 0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(12)
        controls = QWidget()
        content = QVBoxLayout(controls)
        content.setSpacing(10)
        self.library, self.mode = ClickComboBox(), ClickComboBox()
        self.library.addItem("所有启用的资料库", None)
        self.mode.addItem("关键词 · 本地检索", "keyword")
        self.mode.addItem("语义 · Embedding", "semantic")
        self.mode.addItem("混合 · 关键词＋语义", "hybrid")
        buttons(content, self.library, self.mode)
        self.query = QLineEdit()
        self.query.setPlaceholderText("检索资料，不向主聊天发送消息")
        self.query.returnPressed.connect(self._search)
        search, refresh = MotionButton("检索", variant="primary"), MotionButton("刷新资料库", variant="secondary")
        search.clicked.connect(self._search)
        refresh.clicked.connect(self.reload)
        buttons(content, self.query, search, refresh)
        outer.addWidget(CardSurface(controls))
        scope_body = QWidget()
        scope_layout = QVBoxLayout(scope_body)
        self.scope_target = ClickComboBox()
        self.scope_target.addItem("当前项目", "projects")
        self.scope_target.addItem("当前会话", "conversations")
        self.scope_list = QListWidget()
        self.scope_list.setMaximumHeight(140)
        save_scope = MotionButton("保存选中范围", variant="secondary")
        inherit_scope = MotionButton("恢复继承范围", variant="ghost")
        scope_layout.addWidget(label("项目和会话范围取交集；清空选中项表示不允许检索任何库。恢复继承后使用上层范围。", note=True))
        buttons(scope_layout, self.scope_target, save_scope, inherit_scope)
        scope_layout.addWidget(self.scope_list)
        toggle_scope = MotionButton("本次资料范围", variant="ghost")
        toggle_scope.setCheckable(True)
        scope_drawer = AnimatedDisclosure(CardSurface(scope_body), self)
        toggle_scope.toggled.connect(scope_drawer.set_expanded)
        outer.addWidget(toggle_scope)
        outer.addWidget(scope_drawer)
        save_scope.clicked.connect(lambda: self._save_scope(False))
        inherit_scope.clicked.connect(lambda: self._save_scope(True))
        self.scope_target.currentIndexChanged.connect(self._scope_selection)
        self.status = label(note=True)
        outer.addWidget(self.status)
        self.results = QListWidget()
        self.results.setWordWrap(True)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.results.setMaximumHeight(260)
        self.results.itemClicked.connect(self._open)
        outer.addWidget(self.results)
        self.reference = QLineEdit()
        self.reference.setPlaceholderText("粘贴工具结果的chunk_id，定位同版本原文")
        locate = MotionButton("查看引用", variant="secondary")
        locate.clicked.connect(self._locate)
        self.reference.returnPressed.connect(self._locate)
        buttons(outer, self.reference, locate)
        self.source = label(note=True)
        outer.addWidget(self.source)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlaceholderText("检索后点击条目查看原文与来源。正常模型请求仍在主聊天中发送。")
        outer.addWidget(CardSurface(self.text), 1)
        self.previous, self.next, self.stop = MotionButton("前段", variant="secondary"), MotionButton("后段", variant="secondary"), MotionButton("停止", variant="secondary")
        self.previous.setEnabled(False)
        self.next.setEnabled(False)
        self.stop.setEnabled(False)
        self.previous.clicked.connect(lambda: self._read(self._offset - min(6000, self._start_char)))
        self.next.clicked.connect(lambda: self._read(self._next))
        self.stop.clicked.connect(self._stop)
        buttons(outer, self.previous, self.next, self.stop)

    def bind_context(self, context):
        changed = context != self.context
        self.context = context
        if changed:
            self._loaded = False
            self._chunk = self._next = None
            self.results.clear()
            self.text.clear()
            if self.isVisible() and self._worker is None:
                self.reload()

    def close_block(self):
        return self._busy_block()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._loaded:
            self._loaded = True
            self.reload()

    def invalidate(self, _workspace=""):
        self._loaded = False
        if self.isVisible() and self._worker is None:
            self._loaded = True
            self.reload()

    def reload(self):
        selected = self.library.currentData()
        async def operation(cancel, progress):
            return await asyncio.to_thread(self.service.store.libraries, enabled_only=True, limit=1000)
        def receive(data):
            self.library.clear()
            self.library.addItem("所有启用的资料库", None)
            self.scope_list.clear()
            for item in data["libraries"]:
                self.library.addItem(item["name"], item["id"])
                entry = QListWidgetItem(item["name"])
                entry.setData(Qt.ItemDataRole.UserRole, item["id"])
                entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                self.scope_list.addItem(entry)
            self._scope_selection()
            index = self.library.findData(selected)
            if index >= 0:
                self.library.setCurrentIndex(index)
            self.status.setText("资料库已刷新；关闭的库不会被检索。")
            if self.context.resource_id:
                self.reference.setText(self.context.resource_id)
                self._locate()
        self._start(operation, receive)

    def _scope_key(self):
        group = self.scope_target.currentData()
        key = self.service.scopes.project(self.context.workspace) if group == "projects" else self.context.conversation_id
        return group, key

    def _scope_selection(self, *_):
        try:
            group, key = self._scope_key()
            selected = self.service.scopes.load()[group].get(key)
            for index in range(self.scope_list.count()):
                item = self.scope_list.item(index)
                item.setCheckState(Qt.CheckState.Checked if selected is None or item.data(Qt.ItemDataRole.UserRole) in selected else Qt.CheckState.Unchecked)
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))

    def _save_scope(self, inherit):
        if self._worker is not None:
            self.status.setText("请等待当前资料操作结束再修改范围。")
            return
        group, key = self._scope_key()
        selected = [self.scope_list.item(index).data(Qt.ItemDataRole.UserRole) for index in range(self.scope_list.count())
                    if self.scope_list.item(index).checkState() == Qt.CheckState.Checked]
        try:
            self.service.scopes.save(group, key, None if inherit else selected)
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
        else:
            self.results.clear()
            self.text.clear()
            self._chunk = None
            self._scope_selection()
            self.status.setText("资料范围已保存；之后的检索和续读使用新范围，已进入对话的资料不会从历史自动抹除。")

    def _search(self):
        query, library, mode = self.query.text().strip(), self.library.currentData(), self.mode.currentData()
        context = self.context
        if not query or len(query) > 1000:
            self.status.setText("请输入1至1000字的查询。")
            return
        async def operation(cancel, progress):
            return await self.service.search(query, library, 8, mode, workspace=context.workspace, conversation_id=context.conversation_id)
        def receive(data):
            if self.context != context:
                self.status.setText("会话已切换，旧检索结果未展示，请重新检索。")
                return
            self.results.clear()
            self._chunk = self._next = None
            self.previous.setEnabled(False)
            self.next.setEnabled(False)
            self.text.clear()
            self.source.clear()
            for item in data["results"]:
                entry = QListWidgetItem(f"{item['title']} · {item['library']} · 行 {item['start_line']}–{item['end_line']}\n{item['text'][:240]}")
                entry.setData(Qt.ItemDataRole.UserRole, item)
                self.results.addItem(entry)
            coverage = data["coverage"]
            incomplete = not coverage.get("scan_complete", True) or not coverage.get("index_complete", True)
            self.status.setText(f"{len(data['results'])} 个命中" + (" · 部分覆盖，请查看索引状态。" if incomplete else " · 原文资料，请自行核对。"))
        self._start(operation, receive)

    def _open(self, item):
        if self._worker is not None:
            return
        self._chunk = item.data(Qt.ItemDataRole.UserRole)["chunk_id"]
        self._read(0)

    def _locate(self):
        import re
        identifier = self.reference.text().strip()
        if re.fullmatch(r"[0-9a-f]{64}", identifier) is None:
            self.status.setText("请输入工具返回的64位chunk_id。")
            return
        if self._worker is None:
            self._chunk = identifier
            self._read(0)

    def _read(self, offset):
        if self._chunk is None or offset is None:
            return
        identifier = self._chunk
        context = self.context
        async def operation(cancel, progress):
            return await asyncio.to_thread(self.service.read, identifier, offset, 6000, cancel=cancel,
                                           workspace=context.workspace, conversation_id=context.conversation_id)
        def receive(data):
            if self.context != context:
                self.status.setText("会话已切换，旧原文结果未展示。")
                return
            self._offset, self._next = offset, data["next_offset"]
            self._start_char = data["start_char"]
            self.text.setPlainText(data["text"])
            self.source.setText(f"{data['source']}\n字符 {data['start_char']}–{data['end_char']} · " + ("当前资料版本" if data["is_current_revision"] else "历史资料版本"))
            self.previous.setEnabled(data["start_char"] > 0)
            self.next.setEnabled(self._next is not None)
            self.status.setText("已读取原文。")
        self._start(operation, receive)


def create_ui(settings_path, *, services):
    host = services.plugin_host
    service = LibraryService(host.data_directory)
    settings = LibrarySettings(service, services.tasks)
    views = []
    def create(context):
        view = LibraryView(service, services.tasks, context)
        views.append(view)
        def dispose():
            if view in views:
                views.remove(view)
        return UiContribution("rag-view", view, view.close_block, bind_context=view.bind_context,
                              invalidate=view.invalidate, dispose=dispose)
    def changed():
        for view in tuple(views):
            view.invalidate()
    settings.changed.connect(changed)
    return (UiContribution("rag", settings, settings.close_block, slot="model_settings", decorate_rows=decorate),
            UiPageDefinition("rag", "rag", "资料库", ("资料库", "rag"), create, presentation="aside"))
