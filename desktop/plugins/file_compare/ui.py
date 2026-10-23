"""文件比对使用现有辅助页；不应用修改，也不创建模型请求。"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QPlainTextEdit, QVBoxLayout
from ...plugin_api.ui import CardSurface, ClickComboBox, MotionButton, UiContribution, UiPageDefinition, WhiteSurface, theme
from ...ui.common.background_read import BackgroundRead
from .service import compare_files


class DiffColors(QSyntaxHighlighter):
    def highlightBlock(self, text):
        if text.startswith(("+ ", "- ")):
            fmt = QTextCharFormat()
            fmt.setBackground(QColor("#e9f7ee" if text.startswith("+ ") else "#fceceb"))
            fmt.setForeground(QColor(theme.TEXT))
            self.setFormat(0, len(text), fmt)


class ComparePage(WhiteSurface):
    def __init__(self, context):
        super().__init__()
        self.setStyleSheet(f"QListWidget {{ border: none; outline: none; background: {theme.BACKGROUND}; color: {theme.TEXT}; }} QListWidget::item {{ padding: 7px; border-radius: 8px; }} QListWidget::item:selected {{ background: {theme.SELECTED}; color: {theme.TEXT}; }} QPlainTextEdit {{ border: none; background: {theme.BACKGROUND}; color: {theme.TEXT}; selection-background-color: {theme.TEXT_SELECTION}; selection-color: {theme.TEXT}; }}")
        self.context, self._result = context, None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        self.inputs = []
        for title in ("原文件", "新文件／现有change.diff"):
            row = QHBoxLayout()
            field = QLineEdit()
            field.setPlaceholderText(title)
            choose = MotionButton("选择…", variant="secondary")
            choose.clicked.connect(lambda checked=False, target=field: self._choose(target))
            row.addWidget(field, 1)
            row.addWidget(choose)
            layout.addLayout(row)
            self.inputs.append(field)
        row = QHBoxLayout()
        self.encoding = ClickComboBox()
        for label, value in (("自动编码", "auto"), ("UTF-8", "utf-8-sig"), ("GB18030", "gb18030"), ("Shift-JIS", "shift_jis")):
            self.encoding.addItem(label, value)
        self.compare = MotionButton("比对", variant="primary")
        self.patch = MotionButton("查看差异文件", variant="secondary")
        self.compare.clicked.connect(self._compare)
        self.patch.clicked.connect(self._patch)
        for item in (self.encoding, self.compare, self.patch):
            row.addWidget(item)
        layout.addLayout(row)
        self.status = QLabel("只读比较文本；不会回档文件。差异文件可直接查看，PDF／Word请先转换成文本。")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setObjectName("settingsNote")
        layout.addWidget(self.status)
        self.hunks = QListWidget()
        self.hunks.setMaximumHeight(160)
        self.hunks.currentRowChanged.connect(self._show)
        layout.addWidget(self.hunks)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self._colors = DiffColors(self.text.document())
        layout.addWidget(CardSurface(self.text), 1)
        nav = QHBoxLayout()
        for label, delta in (("上一处", -1), ("下一处", 1)):
            button = MotionButton(label, variant="ghost")
            button.clicked.connect(lambda checked=False, shift=delta: self.hunks.setCurrentRow(max(0, min(self.hunks.count() - 1, self.hunks.currentRow() + shift))))
            nav.addWidget(button)
        layout.addLayout(nav)
        self.job = BackgroundRead(self)
        self.job.completed.connect(self._received)
        self.job.failed.connect(self.status.setText)
        self.job.settled.connect(lambda: (self.compare.setEnabled(True), self.patch.setEnabled(True)))

    def _choose(self, field):
        path, _ = QFileDialog.getOpenFileName(self, "选择文本文件", self.context.workspace or "")
        if path:
            field.setText(path)

    def _compare(self):
        left, right = (field.text().strip() for field in self.inputs)
        encoding = self.encoding.currentData()
        if not left or not right:
            self.status.setText("请选择两份文本文件。")
            return
        self._start(lambda: compare_files(left, right, encoding))

    def _patch(self):
        from .service import _read
        path, encoding = self.inputs[1].text().strip(), self.encoding.currentData()
        if not path:
            self.status.setText("请在第二栏选择已有差异文件。")
            return
        def read():
            lines, metadata = _read(path, encoding)
            return {"patch": "".join(lines[:2000]), "metadata": metadata, "truncated": len(lines) > 2000}
        self._start(read)

    def _start(self, function):
        if self.job.worker:
            return
        self.compare.setEnabled(False)
        self.patch.setEnabled(False)
        self.status.setText("正在读取与比较，原文件不变…")
        self.job.start(function)

    def _received(self, result):
        self.hunks.clear()
        self._result = result
        if "patch" in result:
            self.text.setPlainText(result["patch"])
            self.status.setText("已有差异文件；" + ("仅显示前2000行，原文件完整保留。" if result["truncated"] else "已完整读取。"))
            return
        self.hunks.addItems([item["title"] for item in result["hunks"]])
        self.text.clear()
        if result["hunks"]:
            self.hunks.setCurrentRow(0)
        self.status.setText(f"{len(result['hunks'])} 处差异区间" + (" · 已达2000处展示上限。" if not result["complete"] else "。")
                            + (" 字节完全一致。" if result["identical_bytes"] else "")
                            + (" 文本一致，但编码或字节内容不同。" if not result["hunks"] and not result["identical_bytes"] else "")
                            + " 两份文件SHA256：\n" + result["left"]["sha256"] + "\n" + result["right"]["sha256"])

    def _show(self, row):
        if self._result and "hunks" in self._result and 0 <= row < len(self._result["hunks"]):
            self.text.setPlainText(self._result["hunks"][row]["text"])


def create_ui(settings_path, *, services):
    def create(context):
        page = ComparePage(context)
        return UiContribution("file-compare", page, page.job.close_block)
    return (UiPageDefinition("file-compare", "file_compare", "文件比对", ("比对", "diff"), create, presentation="aside"),)
