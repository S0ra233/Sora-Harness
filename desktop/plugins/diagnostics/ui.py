"""诊断辅助页只显示本地事实；正文和密钥不进入该页面。"""
import json
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout
from ...plugin_api.ui import CardSurface, MotionButton, UiContribution, UiPageDefinition, WhiteSurface, theme
from ...ui.common.background_read import BackgroundRead
from .report import conversation_report


class DiagnosticsPage(WhiteSurface):
    def __init__(self, root, context):
        super().__init__()
        self.setStyleSheet(f"QTreeWidget {{ background: {theme.BACKGROUND}; color: {theme.TEXT}; border: none; outline: none; }} QTreeWidget::item {{ padding: 5px; }} QTreeWidget::item:selected {{ background: {theme.SELECTED}; color: {theme.TEXT}; }} QPlainTextEdit {{ border: none; background: {theme.BACKGROUND}; color: {theme.TEXT}; selection-background-color: {theme.TEXT_SELECTION}; selection-color: {theme.TEXT}; }}")
        self.root, self.context = root, context
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        row = QHBoxLayout()
        self.status = QLabel("只读已保存的请求、工具与压缩事实；缺失数据保持未知。")
        self.status.setObjectName("settingsNote")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.refresh_button = MotionButton("刷新", variant="secondary")
        self.refresh_button.clicked.connect(self.refresh)
        row.addWidget(self.status, 1)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        layout.addWidget(CardSurface(self.summary))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["运行／步骤", "状态"])
        self.tree.setUniformRowHeights(True)
        self.tree.currentItemChanged.connect(self._select)
        layout.addWidget(CardSurface(self.tree), 1)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(230)
        layout.addWidget(CardSurface(self.details))
        self.job = BackgroundRead(self)
        self.job.completed.connect(self._receive)
        self.job.failed.connect(self.status.setText)
        self.job.settled.connect(self._settled)
        self._loaded = False

    def _settled(self):
        self.refresh_button.setEnabled(True)
        if getattr(self, "_requested_id", None) != self.context.conversation_id and self.isVisible():
            self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._loaded:
            self.refresh()

    def bind_context(self, context):
        if context != self.context:
            self._loaded = False
            self.tree.clear()
            self.details.clear()
            self.summary.clear()
        self.context = context

    def refresh(self):
        identifier = self.context.conversation_id
        if not identifier or self.job.worker:
            return
        self._requested_id = identifier
        self.refresh_button.setEnabled(False)
        self.status.setText("正在读取已保存的运行事实…")
        self.job.start(lambda: conversation_report(self.root, identifier))

    def _receive(self, data):
        if data["summary"]["conversation_id"] != self.context.conversation_id:
            return
        self._loaded = True
        self.tree.clear()
        summary = data["summary"]
        value = summary["latest_actual_input_after_compaction"]
        self.summary.setText(f"预设：{summary['preset']}\n最近实际输入：{value if value is not None else '未知／压缩后尚无新用量'}\n最近一轮缓存：{summary['latest_run_cache']}\n历史缓存配对样本：{summary['history_paired_cache_steps']} / {summary['history_reported_usage_steps']}")
        def add(parent, row):
            item = QTreeWidgetItem(parent, [str(row["title"]), str(row.get("status", ""))])
            item.setData(0, Qt.ItemDataRole.UserRole, row.get("details", {}))
            for child in row.get("children", []):
                add(item, child)
        add(self.tree, {"title": "统计范围与上下文粗估", "details": {"summary": summary, "estimates": data["estimates"]}})
        for group in data["groups"]:
            add(self.tree, group)
        self.status.setText("\n".join(data["warnings"]) or "读取完成。请求字段变化仅是本地观察，不能证明服务端缓存失效原因。")
        self.tree.setCurrentItem(self.tree.topLevelItem(0))

    def _select(self, item, previous):
        self.details.setPlainText(json.dumps(item.data(0, Qt.ItemDataRole.UserRole), ensure_ascii=False, indent=2) if item else "")


def create_ui(settings_path, *, services):
    def create(context):
        page = DiagnosticsPage(settings_path.parent, context)
        return UiContribution("run-diagnostics", page, page.job.close_block, bind_context=page.bind_context)
    return (UiPageDefinition("run-diagnostics", "diagnostics", "运行诊断", ("诊断", "diagnostics"), create,
                             presentation="aside", scope="conversation", requires_conversation=True),)
