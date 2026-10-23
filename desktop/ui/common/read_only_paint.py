"""只读文档的选中背景；沿用Qt排版和原光标，不覆盖文字前景色。"""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QAbstractTextDocumentLayout, QColor, QPainter, QTextCharFormat, QTextCursor

from ..shell import theme


def draw_read_only_document(editor, event) -> None:
    """按曝光范围画文档；选择、右键复制、搜索和滚动仍由原编辑控件处理。"""
    document = editor.document()
    context = QAbstractTextDocumentLayout.PaintContext()
    context.palette = editor.palette()
    horizontal = editor.horizontalScrollBar()
    x = horizontal.maximum() - horizontal.value() if editor.layoutDirection() == Qt.LayoutDirection.RightToLeft else horizontal.value()
    offset = QPointF(x, editor.verticalScrollBar().value())
    context.clip = QRectF(event.rect()).translated(offset)
    selections = []
    for extra in editor.extraSelections():
        highlight = QAbstractTextDocumentLayout.Selection()
        highlight.cursor, highlight.format = extra.cursor, extra.format
        selections.append(highlight)
    cursor = editor.textCursor()
    background = QTextCharFormat()
    background.setBackground(QColor(theme.TEXT_SELECTION))
    if cursor.hasComplexSelection():
        # 多个表格单元格的选择交回Qt处理，不能把列选择误画成连续正文范围。
        highlight = QAbstractTextDocumentLayout.Selection()
        highlight.cursor = cursor
        highlight.format = background
        selections.append(highlight)
    elif cursor.hasSelection():
        top = editor.cursorForPosition(event.rect().topLeft()).block()
        bottom = editor.cursorForPosition(event.rect().bottomRight()).block()
        first = max(cursor.selectionStart(), min(top.position(), bottom.position()))
        last = min(cursor.selectionEnd(), max(top.position() + top.length(), bottom.position() + bottom.length()))
        block = document.findBlock(first)
        # 每块只选正文，不把前一段的分隔符带进本段；列表标记保持原色与原形。
        # 限定曝光块，选择整份长日志时不逐帧遍历屏幕之外的行。
        while block.isValid() and block.position() < last:
            start = max(first, block.position())
            end = min(last, block.position() + block.length() - 1)
            if start < end:
                highlight = QAbstractTextDocumentLayout.Selection()
                selected = QTextCursor(document)
                selected.setPosition(start)
                selected.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                # Qt值类型先在局部对象上设置，再写入结构体字段，不依赖getter是否返回副本。
                highlight.cursor = selected
                highlight.format = background
                selections.append(highlight)
            block = block.next()
    context.selections = selections
    painter = QPainter(editor.viewport())
    try:
        painter.setClipRegion(event.region())
        painter.translate(-offset.x(), -offset.y())
        document.documentLayout().draw(painter, context)
    finally:
        painter.end()
