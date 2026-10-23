"""整理原生 Markdown 文档的排版；不改消息内容，不加载远程资源。"""
from PySide6.QtCore import Qt
from dataclasses import dataclass
import re
from PySide6.QtGui import (
    QBrush, QColor, QFont, QTextBlockFormat, QTextCharFormat, QTextCursor,
    QTextBlock, QTextFormat, QTextFrameFormat, QTextLength, QTextListFormat, QTextTable,
)

from ..shell import theme


@dataclass(frozen=True)
class DocumentDecorations:
    inline_code: tuple[tuple[int, int], ...]
    markers: tuple[tuple[int, QTextBlock, QTextListFormat.Style], ...]
    tables: tuple[QTextTable, ...]


def _format_tables(frame, font_size: int, *, wrap_lines: bool = False) -> None:
    """表格使用原生框架的边距和单元格格式，不用 QSS 模拟网页表格。"""
    for child in frame.childFrames():
        if isinstance(child, QTextTable):
            style = child.format()
            style.setWidth(QTextLength(QTextLength.Type.PercentageLength, 100))
            if wrap_lines:
                style.setColumnWidthConstraints([QTextLength(QTextLength.Type.PercentageLength, 100 / child.columns())
                                                 for _ in range(child.columns())])
            # 只保留行分隔，表格不再叠加竖向网格或圆角外框。
            style.setBorder(0)
            style.setBorderBrush(QColor(theme.BORDER))
            style.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
            style.setBorderCollapse(True)
            style.setCellSpacing(0)
            style.setCellPadding(8)
            style.setTopMargin(16)
            style.setBottomMargin(16)
            style.setHeaderRowCount(1)
            child.setFormat(style)
            for row in range(child.rows()):
                for column in range(child.columns()):
                    cell = child.cellAt(row, column)
                    cell_style = cell.format().toTableCellFormat()
                    cell_style.setLeftPadding(0 if column == 0 else 8)
                    cell_style.setRightPadding(0 if column == child.columns() - 1 else 8)
                    cell_style.setTopPadding(8)
                    cell_style.setBottomPadding(8)
                    # Markdown导入的单元格有自己的边框／字符格式；只改表格外框不会统一内部网格。
                    cell_style.setBorder(0)
                    cell_style.setBorderBrush(QColor(theme.BORDER))
                    cell_style.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
                    cell_style.setBottomBorder(1)
                    cell_font = cell_style.font()
                    cell_font.setFamilies(theme.TEXT_FAMILIES)
                    cell_font.setPixelSize(font_size)
                    cell_font.setStyleHint(QFont.StyleHint.SansSerif)
                    cell_font.setStyleStrategy(QFont.StyleStrategy.PreferDefault)
                    cell_style.setFont(cell_font)
                    cell_style.setBackground(QColor(theme.BACKGROUND))
                    cell.setFormat(cell_style)
                    if row == 0:
                        cursor = cell.firstCursorPosition()
                        cursor.setPosition(cell.lastCursorPosition().position(), QTextCursor.MoveMode.KeepAnchor)
                        heading = QTextCharFormat()
                        heading.setFontWeight(QFont.Weight.DemiBold)
                        cursor.mergeCharFormat(heading)
        _format_tables(child, font_size, wrap_lines=wrap_lines)


def _bilingual_spacing(document) -> None:
    """字距属于显示格式，原文、复制与 Qt 字符索引不增加空格。"""
    block = document.begin()
    def han(value):
        return bool(value) and "\u3400" <= value <= "\u9fff"
    def latin(value):
        return bool(value) and value.isascii() and value.isalnum()
    while block.isValid():
        text = block.text()
        protected = [(match.start(), match.end()) for match in re.finditer(r"\S*[/\\:@]\S*", text)]
        protected_index = 0
        position = block.position()
        for index, char in enumerate(text):
            following = text[index + 1] if index + 1 < len(text) else ""
            boundary = han(char) and latin(following) or latin(char) and han(following)
            while protected_index < len(protected) and index >= protected[protected_index][1]:
                protected_index += 1
            excluded = protected_index < len(protected) and protected[protected_index][0] <= index
            if boundary and not excluded:
                cursor = QTextCursor(document)
                cursor.setPosition(position)
                cursor.setPosition(position + len(char.encode("utf-16-le")) // 2, QTextCursor.MoveMode.KeepAnchor)
                original = cursor.charFormat()
                monospace = original.fontFixedPitch() or any(name.lower() in {"consolas", "courier new", "monospace"}
                                                             for name in (original.fontFamilies() or ()))
                if not monospace and not original.isAnchor() and not block.blockFormat().hasProperty(QTextFormat.Property.BlockCodeFence):
                    spacing = QTextCharFormat()
                    spacing.setFontLetterSpacingType(QFont.SpacingType.AbsoluteSpacing)
                    spacing.setFontLetterSpacing(theme.BODY_FONT_SIZE * 0.1)
                    cursor.mergeCharFormat(spacing)
            position += len(char.encode("utf-16-le")) // 2
        block = block.next()


def format_document(document, *, markdown: bool, line_height: int,
                    font_size: int = theme.BODY_FONT_SIZE, wrap_lines: bool = False) -> DocumentDecorations:
    """返回行内代码和列表标记，外框引用留空；只在替换或响应结束时完整排版。"""
    inline_code, markers = [], []
    cursor = QTextCursor(document)
    cursor.beginEditBlock()
    try:
        block = document.begin()
        while block.isValid():
            cursor.setPosition(block.position())
            heading = block.blockFormat().headingLevel()
            size = {1: 20, 2: 18, 3: 16}.get(heading, font_size)
            spacing = QTextBlockFormat()
            if wrap_lines:
                # Markdown导入可能留下不可折行格式；正文必须随视口换行，不能只隐藏滚动条。
                spacing.setNonBreakableLines(False)
            spacing.setLineHeight(size * line_height / 100, QTextBlockFormat.LineHeightTypes.FixedHeight.value)
            in_table = cursor.currentTable() is not None
            spacing.setTopMargin(12 if heading and block.previous().isValid() else 0)
            following = block.next()
            text_list = block.textList()
            if text_list is not None:
                list_style = text_list.format().style()
                if (list_style in {QTextListFormat.Style.ListDisc, QTextListFormat.Style.ListCircle, QTextListFormat.Style.ListSquare}
                        and block.blockFormat().marker() == QTextBlockFormat.MarkerType.NoMarker):
                    # 保留原QTextList语义，只把原生靠近正文的标记改为左边界补绘。
                    spacing.setProperty(QTextFormat.Property.ListStyle, QTextListFormat.Style.ListStyleUndefined.value)
                    markers.append((block.position(), block, list_style))
            spacing.setBottomMargin(0 if in_table else 8 if heading
                                    else 0 if block.textList() and following.isValid() and following.textList()
                                    else 12 if markdown and following.isValid() else 0)
            fenced = block.blockFormat().hasProperty(QTextFormat.Property.BlockCodeFence)
            if fenced:
                spacing.setLeftMargin(12)
                spacing.setRightMargin(12)
                spacing.setBackground(QColor(theme.CONTROL))
            cursor.mergeBlockFormat(spacing)
            # 列表标记使用块字符格式，不能只给正文片段设置字体。
            marker = QTextCharFormat()
            marker_font = document.defaultFont()
            marker_font.setPixelSize(size)
            marker.setFont(marker_font)
            marker.setForeground(QColor(theme.TEXT))
            cursor.mergeBlockCharFormat(marker)
            fragments = []
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid():
                    fragments.append((fragment.position(), fragment.length(), fragment.charFormat()))
                next(iterator)
            # 格式写入会改变片段表，因此先记录范围再写入；保持原字符位置供搜索和复制使用。
            for position, length, style in fragments:
                monospace = fenced or style.fontFixedPitch() or any(
                    name.lower() in {"consolas", "courier new", "monospace"} for name in (style.fontFamilies() or ()))
                font = style.font()
                font.setFamilies(["Consolas", "Courier New"] if monospace else theme.TEXT_FAMILIES)
                font.setPixelSize(13 if monospace else size)
                font.setStyleHint(QFont.StyleHint.Monospace if monospace else QFont.StyleHint.SansSerif)
                font.setStyleStrategy(QFont.StyleStrategy.PreferDefault)
                if style.fontWeight() >= QFont.Weight.Bold.value:
                    font.setWeight(QFont.Weight.Bold)
                elif heading or style.fontWeight() >= QFont.Weight.DemiBold.value:
                    font.setWeight(QFont.Weight.DemiBold)
                style.setFont(font)
                style.setForeground(QColor(theme.LINK_COLOR if style.isAnchor() else theme.TEXT))
                if monospace and not fenced:
                    style.setBackground(QBrush(Qt.BrushStyle.NoBrush))
                    # 密集表格只保留等宽字形，不叠加逐词灰色胶囊。
                    if not in_table:
                        if inline_code and inline_code[-1][1] == position:
                            inline_code[-1] = (inline_code[-1][0], position + length)
                        else:
                            inline_code.append((position, position + length))
                cursor.setPosition(position)
                cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(style)
            block = block.next()
        if markdown:
            _format_tables(document.rootFrame(), font_size, wrap_lines=wrap_lines)
        _bilingual_spacing(document)
    finally:
        cursor.endEditBlock()
    return DocumentDecorations(tuple(inline_code), tuple(markers), ())
