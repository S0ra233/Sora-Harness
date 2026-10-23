"""MCP设置复用白色圆角卡片；连接发现只在用户点击后交给后台。"""
from dataclasses import replace
import json
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from ...mcp.client import discover
from ...mcp.settings import McpServer
from ...tools.contracts import ToolError
from ..common.buttons import MotionButton
from ..common.disclosure import AnimatedDisclosure
from ..common.surfaces import CardSurface, WhiteSurface, settings_column
from ..common.switch import SwitchToggle
from ..common.widgets import ClickComboBox
from ..common.auto_save import FormAutoSave
from ..conversations.worker import ConversationWorker
from ..contracts import CloseBlock
from .prompt_page import PromptText


class McpPage(WhiteSurface):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._servers, self._original, self._worker = (), None, None
        self._dirty, self._loading, self._broken = False, False, False
        layout = settings_column(self)
        heading = QLabel("MCP 工具")
        heading.setObjectName("settingsHeading")
        layout.addWidget(heading)
        note = QLabel("接入外部工具服务。保存连接后，点击同步工具；启用的工具用于工作对话，普通聊天不开放。")
        note.setObjectName("settingsDescription")
        note.setWordWrap(True)
        layout.addWidget(note)
        selector = QHBoxLayout()
        self.select = ClickComboBox()
        self.new_button = MotionButton("新建服务", variant="secondary")
        self.new_button.clicked.connect(self._new)
        selector.addWidget(self.select, 1)
        selector.addWidget(self.new_button)
        layout.addLayout(selector)
        self.form_body = QWidget()
        body = QVBoxLayout(self.form_body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(18)
        connection = QWidget()
        form = QFormLayout(connection)
        form.setContentsMargins(22, 20, 22, 20)
        form.setSpacing(14)
        self.name, self.command, self.url, self.cwd = (QLineEdit() for _ in range(4))
        self.name.setPlaceholderText("例如：本地文档、搜索服务")
        self.command.setPlaceholderText("程序名或绝对路径；参数在下方逐行填写")
        self.url.setPlaceholderText("https://…/mcp")
        self.cwd.setPlaceholderText("可选，服务进程的启动目录")
        self.enabled = SwitchToggle("启用 MCP 服务")
        self.transport = ClickComboBox()
        self.transport.addItem("本地程序（stdio）", "stdio")
        self.transport.addItem("远程服务（Streamable HTTP）", "streamable_http")
        enable_body = QWidget()
        enable_row = QHBoxLayout(enable_body)
        enable_row.setContentsMargins(22, 18, 22, 18)
        enable_labels = QVBoxLayout()
        enable_labels.addWidget(QLabel("启用服务"))
        enable_note = QLabel("工作会话可调用这项服务已同步的工具。")
        enable_note.setObjectName("settingsNote")
        enable_labels.addWidget(enable_note)
        enable_row.addLayout(enable_labels, 1)
        enable_row.addWidget(self.enabled)
        body.addWidget(CardSurface(enable_body))
        for title, control in (("名称", self.name), ("连接方式", self.transport),
                               ("启动程序", self.command), ("服务地址", self.url), ("启动目录", self.cwd)):
            form.addRow(title, control)
        self._form = form
        body.addWidget(CardSurface(connection))
        advanced = QWidget()
        fields = QFormLayout(advanced)
        fields.setContentsMargins(20, 16, 20, 18)
        fields.setSpacing(12)
        self.args, self.env, self.headers = (PromptText() for _ in range(3))
        for text in (self.args, self.env, self.headers):
            text.setFixedHeight(80)
        self.args.setPlaceholderText("每行一个参数，不需要添加引号")
        self.env.setPlaceholderText('{"API_KEY": "${MY_MCP_KEY}"}')
        self.headers.setPlaceholderText('{"Authorization": "Bearer ${MY_MCP_KEY}"}')
        self.connect_timeout, self.call_timeout = QLineEdit("20"), QLineEdit("120")
        for label, control in (("启动参数", self.args), ("环境变量（JSON）", self.env),
                               ("请求头（JSON）", self.headers), ("连接时限（秒）", self.connect_timeout),
                               ("调用时限（秒）", self.call_timeout)):
            fields.addRow(label, control)
        self.advanced = AnimatedDisclosure(advanced)
        advanced_card = QWidget()
        advanced_layout = QVBoxLayout(advanced_card)
        advanced_layout.setContentsMargins(16, 12, 16, 12)
        expand = MotionButton("高级连接设置", variant="ghost")
        expand.setCheckable(True)
        expand.toggled.connect(self.advanced.set_expanded)
        advanced_layout.addWidget(expand)
        advanced_layout.addWidget(self.advanced)
        body.addWidget(CardSurface(advanced_card))
        tool_card = QWidget()
        tools = QVBoxLayout(tool_card)
        tools.setContentsMargins(22, 18, 22, 18)
        tools.addWidget(QLabel("已同步工具"))
        self.tool_list = QLabel()
        self.tool_list.setTextFormat(Qt.TextFormat.PlainText)
        self.tool_list.setWordWrap(True)
        self.tool_list.setObjectName("settingsNote")
        tools.addWidget(self.tool_list)
        body.addWidget(CardSurface(tool_card))
        layout.addWidget(self.form_body)
        buttons = QHBoxLayout()
        self.reload_button = MotionButton("重新读取", variant="secondary")
        self.save_button = MotionButton("保存", variant="secondary")
        self.remove_button = MotionButton("移除服务", variant="secondary")
        self.sync_button = MotionButton("连接并同步工具", variant="primary")
        self.sync_button.setObjectName("primaryButton")
        self.cancel_button = MotionButton("停止连接", variant="secondary")
        self.cancel_button.hide()
        for control in (self.reload_button, self.remove_button, self.save_button, self.sync_button, self.cancel_button):
            buttons.addWidget(control)
        layout.addLayout(buttons)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setObjectName("settingsNote")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()
        self.select.currentIndexChanged.connect(self._selected)
        self.transport.currentIndexChanged.connect(self._transport_changed)
        for field in (self.name, self.command, self.url, self.cwd, self.connect_timeout, self.call_timeout):
            field.textChanged.connect(self._mark)
        for field in (self.args, self.env, self.headers):
            field.textChanged.connect(self._mark)
        self.enabled.toggled.connect(self._mark)
        self.transport.currentIndexChanged.connect(self._mark)
        self.save_button.clicked.connect(self._save)
        self.reload_button.clicked.connect(self._reload)
        self.remove_button.clicked.connect(self._remove)
        self.sync_button.clicked.connect(self._sync)
        self.cancel_button.clicked.connect(lambda: self._worker.request_cancel() if self._worker else None)
        self._reload()
        self._auto = FormAutoSave(self, {"name": self.name, "enabled": self.enabled, "transport": self.transport,
            "command": self.command, "url": self.url, "cwd": self.cwd, "args": self.args, "env": self.env,
            "headers": self.headers, "connect_timeout": self.connect_timeout, "call_timeout": self.call_timeout},
            lambda: self._save(automatic=True), self._draft_path(), status=self.status,
            dirty=lambda value: setattr(self, "_dirty", value), ready=lambda: not self._loading and not self._broken and self._worker is None and self._original is not None)
        self._auto.reset()

    def _draft_path(self):
        key = self._original.server_id if self._original and any(item.server_id == self._original.server_id for item in self._servers) else "new"
        return self.settings.path.parent / "ui-drafts" / ("mcp-" + key + ".json")

    def _mark(self, *_):
        if not self._loading:
            if hasattr(self, "_auto"):
                self._auto.changed()

    def _reload(self, *_):
        if hasattr(self, "_auto") and not self._auto.flush():
            return
        try:
            self._servers = self.settings.load()
        except (ValueError, OSError):
            self._broken = True
            self.status.setText("MCP设置读取失败，原文件保留。请修正mcp.json后重新读取。")
            self.form_body.setEnabled(False)
            self.new_button.setEnabled(False)
            for button in (self.save_button, self.sync_button, self.remove_button):
                button.setEnabled(False)
            return
        self._broken = False
        self.new_button.setEnabled(True)
        self.select.blockSignals(True)
        self.select.clear()
        for server in self._servers:
            self.select.addItem(server.name, server.server_id)
        self.select.blockSignals(False)
        self._fill(self._servers[0] if self._servers else None)

    def _fill(self, server):
        self._loading = True
        self._original = server
        self.form_body.setEnabled(server is not None)
        self.save_button.setEnabled(server is not None)
        self.sync_button.setEnabled(server is not None)
        self.remove_button.setEnabled(server is not None and any(item.server_id == server.server_id for item in self._servers))
        if server:
            self.name.setText(server.name)
            self.enabled.setChecked(server.enabled)
            self.transport.setCurrentIndex(self.transport.findData(server.transport))
            for field, value in ((self.command, server.command), (self.url, server.url), (self.cwd, server.cwd),
                                 (self.connect_timeout, str(server.connect_timeout)), (self.call_timeout, str(server.call_timeout))):
                field.setText(value)
            self.args.setPlainText("\n".join(server.args))
            self.env.setPlainText(json.dumps(server.env, ensure_ascii=False, indent=2) if server.env else "")
            self.headers.setPlainText(json.dumps(server.headers, ensure_ascii=False, indent=2) if server.headers else "")
            self.tool_list.setText((f"{len(server.tools)} 个工具\n" + "\n".join(tool["name"] for tool in server.tools))
                                   if server.tools else "尚未同步工具。连接参数改变后需要重新同步。")
        else:
            self.status.setText("暂无MCP服务，点击新建服务配置。")
        self._transport_changed()
        self._loading, self._dirty = False, False
        if hasattr(self, "_auto"):
            self._auto.path = self._draft_path()
            self._auto.reset()

    def _selected(self, index):
        selected = self.select.itemData(index)
        if hasattr(self, "_auto"):
            if not self._auto.flush():
                self.select.blockSignals(True)
                self.select.setCurrentIndex(self.select.findData(self._original.server_id) if self._original else -1)
                self.select.blockSignals(False)
                return
        if self._dirty and not hasattr(self, "_auto"):
            self.status.setText("当前修改尚未保存，请先保存或重新读取。")
            self.select.blockSignals(True)
            self.select.setCurrentIndex(self.select.findData(self._original.server_id) if self._original else -1)
            self.select.blockSignals(False)
            return
        self.select.blockSignals(True)
        self.select.setCurrentIndex(self.select.findData(selected))
        self.select.blockSignals(False)
        self._fill(next((item for item in self._servers if item.server_id == selected), None))

    def _new(self):
        if hasattr(self, "_auto"):
            if not self._auto.flush():
                return
        if self._dirty and not hasattr(self, "_auto"):
            self.status.setText("请先保存当前服务或重新读取，表单已保留。")
            return
        self.select.blockSignals(True)
        self.select.setCurrentIndex(-1)
        self.select.blockSignals(False)
        self._fill(McpServer(uuid4().hex, "新服务", command="uvx"))
        self._dirty = True

    def _transport_changed(self, *_):
        local = self.transport.currentData() == "stdio"
        for widget, visible in ((self.command, local), (self.cwd, local), (self.url, not local)):
            widget.setVisible(visible)
            self._form.labelForField(widget).setVisible(visible)

    def _draft(self):
        return replace(self._original, name=self.name.text().strip(), enabled=self.enabled.isChecked(),
            transport=self.transport.currentData(), command=self.command.text().strip(), url=self.url.text().strip(),
            cwd=self.cwd.text().strip(), args=tuple(self.args.toPlainText().splitlines()),
            env=json.loads(self.env.toPlainText() or "{}"), headers=json.loads(self.headers.toPlainText() or "{}"),
            connect_timeout=float(self.connect_timeout.text()), call_timeout=float(self.call_timeout.text()))

    def _save(self, _checked=False, *, automatic=False):
        try:
            value = self.settings.save(self._draft())
        except (ValueError, TypeError, OSError):
            self.status.setText("保存失败。请检查连接、JSON键值和时限；原设置与当前表单保留。")
            return False
        if automatic:
            self._original = value
            self._servers = self.settings.load()
            self.select.blockSignals(True)
            index = self.select.findData(value.server_id)
            if index < 0:
                self.select.addItem(value.name, value.server_id)
                index = self.select.count() - 1
            self.select.setItemText(index, value.name)
            self.select.setCurrentIndex(index)
            self.select.blockSignals(False)
            self.remove_button.setEnabled(True)
            self.tool_list.setText(f"{len(value.tools)} 个工具；连接变更后需主动重新同步。")
        else:
            self._auto.reset(restore=False)
            self._reload()
            self.select.setCurrentIndex(self.select.findData(value.server_id))
        self.status.setText("已保存，后续工作请求使用新配置；正在运行的任务保留原快照。")
        return True

    def _remove(self):
        try:
            self.settings.remove(self._original.server_id)
        except (ValueError, OSError):
            self.status.setText("移除未保存，原设置保留。")
            return
        self._auto.reset(restore=False)
        self._reload()

    def _sync(self):
        if not self._auto.flush():
            return
        try:
            server = self._draft()
        except (ValueError, TypeError):
            self.status.setText("请先检查连接、JSON键值和时限，尚未建立连接。")
            return
        async def operation(_events):
            try:
                tools = await discover(server)
            except ToolError:
                raise
            except Exception as exc:
                raise ValueError(f"连接或工具同步失败（{type(exc).__name__}），旧目录保留。") from exc
            return replace(server, tools=tools)
        self._worker = ConversationWorker(operation, self)
        self._worker.progress.connect(self._progress)
        self._worker.finished.connect(self._finished)
        for widget in (self.form_body, self.select, self.new_button, self.reload_button, self.save_button, self.remove_button, self.sync_button):
            widget.setEnabled(False)
        self.cancel_button.show()
        self.status.setText("正在连接并读取工具目录…")
        self._worker.start()

    def _progress(self, event):
        if event.kind == "completed":
            try:
                value = self.settings.save(event.data, synced=True)
                self._auto.reset(restore=False)
                self._reload()
                self.select.setCurrentIndex(self.select.findData(value.server_id))
                self.status.setText(f"已同步 {len(value.tools)} 个工具并保存。")
            except (ValueError, OSError):
                self.status.setText("工具已取得，但设置保存失败；未更新旧目录，请重试保存。")
        elif event.kind in {"failed", "cancelled"}:
            self.status.setText(event.error or "已停止连接，旧工具目录保留。")

    def _finished(self):
        worker, self._worker = self._worker, None
        worker.deleteLater()
        self.cancel_button.hide()
        for widget in (self.select, self.new_button, self.reload_button):
            widget.setEnabled(not self._broken)
        self.form_body.setEnabled(self._original is not None and not self._broken)
        for widget in (self.save_button, self.sync_button):
            widget.setEnabled(self._original is not None and not self._broken)
        self.remove_button.setEnabled(self._original is not None and any(item.server_id == self._original.server_id for item in self._servers))

    def close_block(self):
        if self._worker is not None:
            return CloseBlock("MCP正在连接", "请等待同步完成或先停止连接。")
        return self._auto.close_block()
