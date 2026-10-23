"""从显示数据逐项建立搜索文档，不为检索创建历史消息控件。"""
from PySide6.QtGui import QTextDocument
from .code_message import display_parts, split_fenced_code
from .tools.models import present_tool


class _SearchDocument(QTextDocument):
    def loadResource(self, kind, url):
        return None


def _sources(row):
    if row.activity or row.page_reference:
        return
    if row.tools:
        for tool in row.tools:
            yield tool.call_id, present_tool(tool).body, False
    elif row.reasoning:
        yield None, row.text, not row.reasoning_running
    elif not row.turn_id:
        parts = display_parts(row.text) if row.markdown else split_fenced_code(row.text)
        if len(parts) > 1 or any(part.language is not None for part in parts):
            for part in parts:
                yield part.key, part.text, row.markdown and part.language is None
        else:
            yield None, row.text, row.markdown
    else:
        yield None, row.text, True


def search_documents(rows, parent):
    for row in rows:
        children = row.process.rows if row.process is not None else (row,)
        for child in children:
            for target, text, markdown in _sources(child):
                document = _SearchDocument(parent)
                document.setLayoutEnabled(False)
                if markdown:
                    document.setMarkdown(text, QTextDocument.MarkdownFeature.MarkdownDialectGitHub | QTextDocument.MarkdownFeature.MarkdownNoHTML)
                else:
                    document.setPlainText(text)
                address = (child.key, target) if row.process is not None else target
                try:
                    yield row.key, address, document
                finally:
                    # 逐项扫描后即释放；不把整段会话的临时文档留在父对象树中。
                    document.deleteLater()
