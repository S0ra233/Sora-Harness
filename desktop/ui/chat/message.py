"""一条显示行的绘制；流式追加纯文本，完整响应才解析 Markdown。"""
import math
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import datetime

from PySide6.QtCore import QAbstractAnimation, QEvent, QEasingCurve, Property, QPropertyAnimation, QRectF, Qt, QSize, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QImageReader, QPainter, QPainterPath, QPen, QPixmap, QTextCursor, QTextDocument, QTextListFormat, QTextOption
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QSizePolicy, QTextBrowser, QVBoxLayout, QWidget

from ...attachments.store import ImageStore
from ..shell import theme
from .projection import DisplayRow
from .tools.group import ToolGroup
from .tools.models import ToolGroupState
from ..common.widgets import highlight_text, IconGlyph, IconButton
from ..common.status_text import StatusSpinner, StatusText
from ..common.copy_button import CopyButton
from ..common.icons import icon
from .code_message import CodeMessage, FencePartition
from ..common.buttons import MotionButton
from ..common.surfaces import FadeVeil
from ..common.read_only_paint import draw_read_only_document
from .markdown import format_document
from .reasoning_card import ReasoningCard


@dataclass
class MessageViewState:
    expanded: bool = False
    code_positions: dict[str, tuple[int, int]] = field(default_factory=dict)
    reasoning_expanded: bool | None = None


class MessageText(QTextBrowser):
    """由父滚动区滚动正文；Markdown 不能自动读取外部文件或远程图片。"""

    interacted = Signal()
    natural_height_changed = Signal(int)
    geometry_changed = Signal()

    def __init__(self, parent=None, *, compact: bool = False, wrap_body: bool = False) -> None:
        super().__init__(parent)
        self._wrap_body = wrap_body
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff if wrap_body else Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setLineWrapMode(QTextBrowser.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setStyleSheet(f"QTextBrowser {{ background: transparent; color: {theme.TEXT}; border: none; padding: 0; margin: 0; }}")
        font = self.font()
        font.setFamilies(theme.TEXT_FAMILIES)
        self._body_size = 12 if compact else theme.BODY_FONT_SIZE
        font.setPixelSize(self._body_size)
        font.setWeight(QFont.Weight.Normal)
        font.setStyleHint(QFont.StyleHint.SansSerif)
        font.setStyleStrategy(QFont.StyleStrategy.PreferDefault)
        self.setFont(font)
        self.document().setDefaultFont(font)
        self.document().setDocumentMargin(0)
        # 使用原生列表缩进分开标记和正文；不插入空格，以免改变搜索字符位置。
        self.document().setIndentWidth(26)
        self.anchorClicked.connect(self._open_link)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._natural_height = 0
        self._updating_content = False
        self._height_limit: int | None = None
        self._animation = QPropertyAnimation(self, b"displayHeight", self)
        self._animation.setDuration(theme.DISCLOSURE_DURATION_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.document().documentLayout().documentSizeChanged.connect(self._fit)
        self._text = ""
        self._markdown = False
        self._line_height = 165 if compact else theme.BODY_LINE_HEIGHT
        self._inline_code = ()
        self._markers = self._tables = self._table_rects = ()
        self._decoration_key = None
        self._format_document()

    def loadResource(self, kind, url):
        return None

    def set_content(self, text: str, markdown: bool) -> bool:
        if text == self._text and markdown == self._markdown:
            return False
        self._updating_content = True
        try:
            if not markdown and not self._markdown and text.startswith(self._text):
                cursor = QTextCursor(self.document())
                cursor.movePosition(QTextCursor.MoveOperation.End)
                cursor.insertText(text[len(self._text):])
            else:
                document = self.document()
                layout_enabled = document.isLayoutEnabled()
                # 完整解析与各段格式一起提交，避免每个片段都重新计算高度和滚动位置。
                document.setLayoutEnabled(False)
                try:
                    self._markers = self._tables = self._table_rects = ()
                    self._decoration_key = None
                    if markdown:
                        document.setMarkdown(text, QTextDocument.MarkdownFeature.MarkdownDialectGitHub | QTextDocument.MarkdownFeature.MarkdownNoHTML)
                    else:
                        self.setPlainText(text)
                    self._format_document(markdown=markdown)
                finally:
                    document.setLayoutEnabled(layout_enabled)
        finally:
            self._updating_content = False
        self._text, self._markdown = text, markdown
        self._fit()
        return True

    def _format_document(self, *, markdown: bool = False) -> None:
        decoration = format_document(self.document(), markdown=markdown, line_height=self._line_height,
                                     font_size=self._body_size, wrap_lines=self._wrap_body)
        self._inline_code, self._markers, self._tables = decoration.inline_code, decoration.markers, decoration.tables
        self._decoration_key = None

    def _open_link(self, url) -> None:
        # 仅响应用户点击网页链接；Markdown中的本地路径或其他协议不能触发程序执行。
        if url.scheme().lower() in {"http", "https"}:
            QDesktopServices.openUrl(url)

    def paintEvent(self, event) -> None:
        # 只画底色，原文本仍由Qt绘制和选择；paintEvent中不修改文档。
        if self._inline_code:
            painter = QPainter(self.viewport())
            try:
                painter.setClipRegion(event.region())
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#f2f2f2"))
                cursor = QTextCursor(self.document())
                first_visible = self.cursorForPosition(event.rect().topLeft()).position()
                last_visible = self.cursorForPosition(event.rect().bottomRight()).position()
                first_visible, last_visible = sorted((first_visible, last_visible))
                first_index = max(0, bisect_left(self._inline_code, (first_visible,)) - 1)
                # 绘制只扫描当前曝光范围，滚动长消息不逐帧遍历全部代码胶囊。
                for index in range(first_index, len(self._inline_code)):
                    start, end = self._inline_code[index]
                    if start > last_visible:
                        break
                    if end < first_visible:
                        continue
                    block = self.document().findBlock(start)
                    text_layout = block.layout()
                    if text_layout.lineCount() == 0:
                        continue
                    first = text_layout.lineAt(0)
                    cursor.setPosition(block.position())
                    origin = self.cursorRect(cursor)
                    # PySide的cursorToX返回(x, 修正后位置)，此处只取x坐标。
                    dx, dy = origin.x() - first.cursorToX(0)[0], origin.y() - first.y()
                    for index in range(text_layout.lineCount()):
                        line = text_layout.lineAt(index)
                        left = max(start - block.position(), line.textStart())
                        right = min(end - block.position(), line.textStart() + line.textLength())
                        if right <= left:
                            continue
                        x1, x2 = line.cursorToX(left)[0], line.cursorToX(right)[0]
                        # 字符流没有额外padding，底色仅外扩1px，避免盖到列表标记或相邻标点。
                        rect = QRectF(dx + min(x1, x2), dy + line.y(), abs(x2 - x1), line.height()).adjusted(-1, 0, 1, 0)
                        if rect.intersects(QRectF(event.rect())):
                            painter.drawRoundedRect(rect, 4, 4)
            finally:
                painter.end()
        if self.textCursor().hasSelection():
            draw_read_only_document(self, event)
        else:
            super().paintEvent(event)
        if self._markers or self._table_rects:
            self._paint_decorations(event)

    def _paint_decorations(self, event) -> None:
        painter = QPainter(self.viewport())
        try:
            painter.setClipRegion(event.region())
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.TEXT))
            first = self.cursorForPosition(event.rect().topLeft()).block().position()
            last_block = self.cursorForPosition(event.rect().bottomRight()).block()
            last = last_block.position() + last_block.length()
            cursor = QTextCursor(self.document())
            for marker_index in range(bisect_left(self._markers, (first,)), len(self._markers)):
                position, block, style = self._markers[marker_index]
                if position > last:
                    break
                if not block.isValid() or block.layout().lineCount() == 0:
                    continue
                cursor.setPosition(position)
                origin = self.cursorRect(cursor)
                size = self.fontMetrics().lineSpacing() / 3
                rect = QRectF(origin.x() - self.document().indentWidth(), origin.y() + (self.fontMetrics().height() - size) / 2, size, size)
                painter.setBrush(Qt.BrushStyle.NoBrush if style == QTextListFormat.Style.ListCircle else QColor(theme.TEXT))
                painter.setPen(QPen(QColor(theme.TEXT), 1) if style == QTextListFormat.Style.ListCircle else Qt.PenStyle.NoPen)
                if style == QTextListFormat.Style.ListSquare:
                    painter.drawRect(rect)
                else:
                    painter.drawEllipse(rect)
            offset_x, offset_y = self.horizontalScrollBar().value(), self.verticalScrollBar().value()
            for bounds in self._table_rects:
                rect = bounds.translated(-offset_x, -offset_y)
                if not rect.intersects(QRectF(event.rect())):
                    continue
                radius = min(10.0, rect.width() / 2, rect.height() / 2)
                square, rounded = QPainterPath(), QPainterPath()
                square.addRect(rect)
                rounded.addRoundedRect(rect, radius, radius)
                # 局部覆盖四个角，原表格布局、单元格选择与文字仍交给Qt。
                painter.fillPath(square.subtracted(rounded), QColor(theme.BACKGROUND))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(theme.BORDER), 1))
                painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        finally:
            painter.end()

    def _fit(self, _size=None) -> None:
        if self._updating_content:
            return
        size = self.document().size()
        key = (self.document().revision(), self.document().textWidth(), size.width(), size.height())
        if key != self._decoration_key:
            layout = self.document().documentLayout()
            self._table_rects = tuple(layout.frameBoundingRect(table).adjusted(table.format().leftMargin(), table.format().topMargin(),
                                     -table.format().rightMargin(), -table.format().bottomMargin()) for table in self._tables)
            self._decoration_key = key
        horizontal = 8 if not self._wrap_body and size.width() > self.viewport().width() + 1 else 0
        height = math.ceil(size.height()) + self.frameWidth() * 2 + horizontal
        height = max(self.fontMetrics().height(), height)
        changed = self._natural_height != height
        self._natural_height = height
        target = min(height, self._height_limit) if self._height_limit is not None else height
        if self._animation.state() == QAbstractAnimation.State.Running:
            if self._animation.endValue() != target:
                self._animation.setEndValue(target)
        elif self.height() != target:
            self.displayHeight = target
        if changed:
            self.natural_height_changed.emit(height)

    @property
    def natural_height(self) -> int:
        return self._natural_height

    @Property(int)
    def displayHeight(self) -> int:
        return self.height()

    @displayHeight.setter
    def displayHeight(self, value: int) -> None:
        if self.height() != value:
            self.setFixedHeight(value)
            self.geometry_changed.emit()

    def set_height_limit(self, height: int | None, *, animate: bool = False) -> None:
        if height == self._height_limit:
            return
        self._height_limit = height
        self._animation.stop()
        target = min(self._natural_height, height) if height is not None else self._natural_height
        if animate and self.isVisible():
            full = max(1, self._natural_height, self.height(), target)
            self._animation.setDuration(max(1, round(theme.DISCLOSURE_DURATION_MS * abs(target - self.height()) / full)))
            self._animation.setStartValue(self.height())
            self._animation.setEndValue(target)
            self._animation.start()
        else:
            self._fit()
        if height is not None:
            self.verticalScrollBar().setValue(0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        width = max(1, self.viewport().width())
        if self.document().textWidth() != width:
            self.document().setTextWidth(width)
        self._fit()

    def wheelEvent(self, event) -> None:
        if not self._wrap_body and (event.modifiers() & Qt.KeyboardModifier.ShiftModifier or event.angleDelta().x() or event.pixelDelta().x()):
            super().wheelEvent(event)
            return
        # 正文已按文档高度展开，滚轮交给外层会话滚动区，避免嵌套滚动截获。
        event.ignore()

    def mousePressEvent(self, event) -> None:
        self.interacted.emit()
        super().mousePressEvent(event)


class MessageBlock(QFrame):
    interacted = Signal()
    layout_changed = Signal()
    action_requested = Signal(str, object, object)
    page_requested = Signal(str, str)

    def __init__(self, row: DisplayRow, images: ImageStore, parent=None, *,
                 tool_state: ToolGroupState | None = None, view_state: MessageViewState | None = None) -> None:
        super().__init__(parent)
        self.row: DisplayRow | None = None
        self._images = images
        self._image_ids: tuple[str, ...] = ()
        self._image_labels: list[QLabel] = []
        self._natural_width = 44
        self._tool_group: ToolGroup | None = None
        self._activity: QWidget | None = None
        self._activity_text: StatusText | None = None
        self._activity_spinner: StatusSpinner | None = None
        self._tool_state = tool_state or ToolGroupState()
        self._view_state = view_state or MessageViewState()
        self._code_message: CodeMessage | None = None
        self._fences = FencePartition()
        self._reasoning_card: ReasoningCard | None = None
        self._page_link = None
        self._fold_limit = 300
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self._bubble_layout = QHBoxLayout()
        self._bubble_layout.setContentsMargins(0, 0, 0, 0)
        self._bubble_layout.setSpacing(0)
        self._surface = QFrame()
        self._surface.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._contents = QVBoxLayout(self._surface)
        self._contents.setSpacing(8)
        self._bubble_layout.addStretch()
        self._bubble_layout.addWidget(self._surface, 1)
        layout.addLayout(self._bubble_layout)
        self.label = QLabel()
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setStyleSheet(f"color: {theme.FAINT}; font-size: 11px; background: transparent;")
        self.text = MessageText(wrap_body=row.turn_id is None and not row.reasoning and not row.tools and not row.activity)
        self._contents.addWidget(self.label)
        self._contents.addWidget(self.text)
        self.text.interacted.connect(self.interacted.emit)
        self.text.natural_height_changed.connect(self._text_measured)
        self.text.geometry_changed.connect(self.layout_changed.emit)
        self._more = MotionButton("显示更多", variant="ghost", icon_position="right")
        self._more.setObjectName("messageMore")
        self._more.setCursor(Qt.CursorShape.PointingHandCursor)
        self._more.clicked.connect(self._toggle_message)
        self._contents.addWidget(self._more, 0, Qt.AlignmentFlag.AlignLeft)
        self._more.hide()
        self._actions = QWidget()
        self._actions.setFixedHeight(24)
        actions = QHBoxLayout(self._actions)
        actions.setContentsMargins(2, 0, 2, 0)
        actions.setSpacing(6)
        if row.turn_id:
            actions.addStretch()
        self._time = QLabel()
        self._time.setTextFormat(Qt.TextFormat.PlainText)
        self._time.setStyleSheet(f"color: {theme.FAINT}; font-size: 11px; background: transparent;")
        self._copy = CopyButton(lambda: self.row.text)
        self._rewind = IconButton("undo", "回退到此消息")
        self._branch = IconButton("branch", "从此消息创建分支")
        self._edit = IconButton("edit", "编辑回答")
        self._regenerate = IconButton("refresh", "重新执行本轮")
        self._rewind.clicked.connect(lambda: self.action_requested.emit("rewind", self.row.anchor, None))
        self._branch.clicked.connect(lambda: self.action_requested.emit("fork", self.row.anchor, None))
        self._edit.clicked.connect(self._begin_edit)
        self._regenerate.clicked.connect(lambda: self.action_requested.emit("regenerate", self.row.anchor, None))
        self._editor = None
        self._edit_buttons = None
        actions.addWidget(self._time)
        for button in (self._rewind, self._branch, self._edit, self._regenerate):
            actions.addWidget(button)
            button.installEventFilter(self)
        actions.addWidget(self._copy)
        if not row.turn_id:
            actions.addStretch()
        self._copy.installEventFilter(self)
        self._actions.installEventFilter(self)
        self._action_veil = FadeVeil(self._actions)
        self._action_veil.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._action_veil.opacity = 1.0
        self._action_veil.show()
        self._action_fade = QPropertyAnimation(self._action_veil, b"opacity", self)
        self._action_fade.setDuration(100)
        self._action_fade.setEasingCurve(QEasingCurve.Type.Linear)
        self._action_fade.finished.connect(self._finish_action_fade)
        layout.addWidget(self._actions)
        self.update_row(row)

    def update_row(self, row: DisplayRow) -> bool:
        if self.row == row:
            return False
        previous = self.row
        if previous is not None and self._editor is not None and previous.text != row.text:
            self._cancel_edit()
        changed = previous != row
        self.row = row
        if row.page_reference is not None:
            self.label.hide()
            self.text.hide()
            self._more.hide()
            self._actions.hide()
            self._contents.setContentsMargins(0, 0, 0, 0)
            self._bubble_layout.setStretch(0, 0)
            if self._page_link is None:
                self._page_link = MotionButton(variant="secondary")
                self._page_link.setIcon(icon("plugin", theme.MUTED, 14))
                self._page_link.setStyleSheet("QPushButton { color: #8b8e94; background: transparent; border: none; border-radius: 8px; padding: 3px 6px; text-align: left; } QPushButton:hover { background: #f4f4f4; }")
                self._page_link.clicked.connect(lambda: self.page_requested.emit(*self.row.page_reference))
                self._contents.addWidget(self._page_link, 0, Qt.AlignmentFlag.AlignLeft)
            self._page_link.setText(row.text)
            return changed
        if row.reasoning:
            self._more.hide()
            self.text.hide()
            self.label.hide()
            self._actions.hide()
            if self._reasoning_card is None:
                self._reasoning_card = ReasoningCard(row, self._view_state, MessageText, self._surface)
                self._reasoning_card.interacted.connect(self.interacted.emit)
                self._reasoning_card.layout_changed.connect(self.layout_changed.emit)
                self._contents.addWidget(self._reasoning_card)
            else:
                self._reasoning_card.update_row(row)
            return True
        if previous is None or previous.label != row.label:
            self.label.setText(row.label)
            self.label.setVisible(bool(row.label) and row.label not in {"你", "模型"} and not row.tools and not row.activity)
        if previous is None or bool(previous.text) != bool(row.text):
            self.text.setVisible(bool(row.text) and not row.activity)
        if previous is None:
            notice = row.key.startswith(("tool:", "notice:", "summary:"))
            name = "activitySurface" if row.activity else "toolSurface" if row.tools else "userBubble" if row.turn_id else "messageNotice" if notice else "assistantSurface"
            self._surface.setObjectName(name)
            self._contents.setContentsMargins(16 if row.turn_id else 12 if notice else 0,
                                             10 if row.turn_id else 12 if notice else 0,
                                             16 if row.turn_id else 12 if notice else 0,
                                             10 if row.turn_id else 12 if notice else 0)
            self._bubble_layout.setStretch(0, 1 if row.turn_id else 0)
        if previous is None or (previous.text, previous.markdown) != (row.text, row.markdown):
            parts = self._fences.update(row.text, row.markdown) if not row.turn_id and not row.tools and not row.activity else ()
            if len(parts) > 1 or any(part.language is not None for part in parts):
                if self._code_message is None:
                    self._code_message = CodeMessage(parts, lambda: MessageText(wrap_body=True), self._view_state.code_positions,
                                                     self._surface, markdown=row.markdown)
                    self._code_message.interacted.connect(self.interacted.emit)
                    self._code_message.layout_changed.connect(self.layout_changed.emit)
                    self._contents.insertWidget(2, self._code_message)
                else:
                    self._code_message.set_parts(parts, markdown=row.markdown)
                self.text.set_content("", False)
                self.text.hide()
            else:
                if self._code_message is not None:
                    self._view_state.code_positions = self._code_message.positions()
                    self._contents.removeWidget(self._code_message)
                    self._code_message.hide()
                    self._code_message.deleteLater()
                    self._code_message = None
                changed = self.text.set_content(row.text, row.markdown or bool(row.turn_id)) or changed
                self.text.setVisible(bool(row.text) and not row.activity)
        self._refresh_fold()
        self._copy.setVisible(row.copyable or bool(row.turn_id))
        self._rewind.setVisible(row.turn_id is not None and row.anchor is not None and row.rewindable)
        self._branch.setVisible(row.turn_id is not None and row.anchor is not None)
        self._edit.setVisible(row.editable)
        self._regenerate.setVisible(row.editable and row.regeneratable)
        try:
            instant = datetime.fromisoformat(row.timestamp).astimezone() if row.timestamp else None
            timestamp = instant.strftime("%H:%M") if instant else ""
            self._time.setToolTip(instant.strftime("%Y年%m月%d日 %H:%M") if instant else "")
        except ValueError:
            timestamp = ""
        self._time.setText(timestamp)
        self._time.setVisible(bool(timestamp))
        self._actions.setVisible((row.copyable or row.editable or bool(row.turn_id) or bool(timestamp)) and bool(row.text or row.images)
                                 and not row.tools and not row.activity)
        if row.activity:
            if self._activity is None:
                self._activity = QWidget()
                status_layout = QHBoxLayout(self._activity)
                status_layout.setContentsMargins(0, 0, 0, 0)
                status_layout.setSpacing(8)
                if row.activity in {"working", "settled"}:
                    self._activity_spinner = StatusSpinner()
                    status_layout.addWidget(self._activity_spinner)
                else:
                    status_layout.addWidget(IconGlyph("compact", size=16, color=theme.TOOL_TEXT))
                self._activity_text = StatusText()
                status_layout.addWidget(self._activity_text, 1)
                self._contents.addWidget(self._activity)
            self._activity_text.set_status(row.text, row.activity in {"running", "working"})
            if self._activity_spinner is not None:
                self._activity_spinner.setVisible(row.activity == "working")
                self._activity_spinner.set_active(row.activity == "working")
            self._activity.setToolTip(row.text)
            self._activity.setAccessibleName(row.text)
        if row.tools and (previous is None or previous.tools != row.tools):
            if self._tool_group is None:
                self._tool_group = ToolGroup(row.tools, self._tool_state, self._surface)
                self._tool_group.interacted.connect(self.interacted.emit)
                self._tool_group.layout_changed.connect(self.layout_changed.emit)
                self._contents.addWidget(self._tool_group)
            else:
                self._tool_group.set_tools(row.tools)
        ids = tuple(item.attachment_id for item in row.images)
        if ids != self._image_ids:
            for label in self._image_labels:
                self._contents.removeWidget(label)
                label.deleteLater()
            self._image_labels = []
            self._image_ids = ids
            for reference in row.images:
                label = QLabel()
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setWordWrap(True)
                try:
                    info = self._images.metadata(reference)
                    reader = QImageReader(str(self._images.image_path(reference)))
                    reader.setScaledSize(QSize(info.width, info.height).scaled(360, 240, Qt.AspectRatioMode.KeepAspectRatio))
                    image = reader.read()
                    if image.isNull():
                        raise ValueError("预览图无法解码。")
                    label.setPixmap(QPixmap.fromImage(image))
                    label.setToolTip(info.name)
                except (OSError, ValueError) as exc:
                    label.setText(f"图片 {reference.attachment_id}：{exc}")
                self._contents.addWidget(label)
                self._image_labels.append(label)
            changed = True
        if row.turn_id and (previous is None or previous.text != row.text or previous.images != row.images):
            # 用户消息不会逐段生成；每次实际内容变化才测量最长行／缩略图宽度。
            text_width = max((self.text.fontMetrics().horizontalAdvance(line) for line in self.text.document().toPlainText().splitlines()), default=0)
            image_width = max((label.pixmap().width() for label in self._image_labels if label.pixmap() is not None), default=0)
            self._natural_width = max(44, text_width + 34, image_width + 34)
            self._resize_bubble()
        return changed

    def save_tool_state(self) -> ToolGroupState | None:
        return self._tool_group.save_state() if self._tool_group is not None else None

    def save_view_state(self) -> MessageViewState:
        if self._code_message is not None:
            self._view_state.code_positions = self._code_message.positions()
        return self._view_state

    def set_viewport_height(self, height: int) -> None:
        line_height = theme.BODY_FONT_SIZE * theme.BODY_LINE_HEIGHT / 100
        self._fold_limit = min(round(line_height * theme.USER_COLLAPSE_LINES), max(120, round(height * 0.6)))
        self._refresh_fold()

    def _text_measured(self, _height: int) -> None:
        self._refresh_fold()

    def _refresh_fold(self, *, animate: bool = False) -> None:
        if self.row is None or not self.row.turn_id:
            return
        long = self.text.natural_height > self._fold_limit + 1
        self._more.setVisible(long)
        self._more.setText("收起" if self._view_state.expanded else "显示更多")
        self._more.setIcon(icon("up" if self._view_state.expanded else "chevron"))
        self.text.set_height_limit(self._fold_limit if long and not self._view_state.expanded else None, animate=animate)

    def _toggle_message(self) -> None:
        self.interacted.emit()
        self._view_state.expanded = not self._view_state.expanded
        self._refresh_fold(animate=True)

    def enterEvent(self, event) -> None:
        self._fade_actions(True)
        super().enterEvent(event)

    def _begin_edit(self) -> None:
        if self._editor is not None:
            return
        self.interacted.emit()
        self._editor = QPlainTextEdit(self.row.text, self._surface)
        self._editor.setMinimumHeight(120)
        self._editor.setMaximumHeight(320)
        self._editor.setStyleSheet(f"background: {theme.BACKGROUND}; color: {theme.TEXT}; border: 1px solid {theme.BORDER}; border-radius: 12px; padding: 12px;")
        self._edit_buttons = QWidget(self._surface)
        buttons = QHBoxLayout(self._edit_buttons)
        buttons.addStretch()
        cancel, save = MotionButton("取消", variant="ghost"), MotionButton("保存", variant="primary")
        save.setObjectName("primaryButton")
        cancel.clicked.connect(self._cancel_edit)
        save.clicked.connect(lambda: self.action_requested.emit("edit", self.row.anchor, self._editor.toPlainText()))
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        self._contents.addWidget(self._editor)
        self._contents.addWidget(self._edit_buttons)
        self.text.hide()
        if self._code_message is not None:
            self._code_message.hide()
        self._actions.hide()
        self._editor.setFocus()
        self.layout_changed.emit()

    def _cancel_edit(self) -> None:
        for widget in (self._editor, self._edit_buttons):
            if widget is not None:
                self._contents.removeWidget(widget)
                widget.deleteLater()
        self._editor = self._edit_buttons = None
        self.text.setVisible(self._code_message is None and bool(self.row.text))
        if self._code_message is not None:
            self._code_message.show()
        self._actions.show()
        self.layout_changed.emit()

    def leaveEvent(self, event) -> None:
        if not any(button.hasFocus() for button in (self._copy, self._rewind, self._branch, self._edit, self._regenerate)):
            self._fade_actions(False)
        super().leaveEvent(event)

    def _fade_actions(self, visible: bool) -> None:
        if not self._actions.isVisible():
            return
        self._action_fade.stop()
        self._action_veil.show()
        self._action_veil.raise_()
        self._action_fade.setStartValue(self._action_veil.opacity)
        self._action_fade.setEndValue(0.0 if visible else 1.0)
        self._action_fade.start()

    def _finish_action_fade(self) -> None:
        if self._action_veil.opacity < 0.01:
            self._action_veil.hide()

    def eventFilter(self, watched, event) -> bool:
        if watched is self._actions and event.type() == QEvent.Type.Resize:
            self._action_veil.setGeometry(self._actions.rect())
        elif watched in {self._copy, self._rewind, self._branch, self._edit, self._regenerate} and event.type() in {QEvent.Type.FocusIn, QEvent.Type.FocusOut}:
            self._fade_actions(event.type() == QEvent.Type.FocusIn or self.underMouse())
        return super().eventFilter(watched, event)

    def hideEvent(self, event) -> None:
        self._action_fade.stop()
        self._action_veil.opacity = 1.0
        self._action_veil.show()
        super().hideEvent(event)

    def documents(self):
        if self._reasoning_card is not None:
            return ((None, self._reasoning_card.document()),)
        if self._tool_group is not None:
            return self._tool_group.documents()
        if self._code_message is not None:
            return self._code_message.documents()
        return () if self.row.activity else ((None, self.text.document()),)

    def reading_part(self, viewport):
        return self._code_message.reading_anchor(viewport) if self._code_message is not None else None

    def part_widget(self, key):
        return self._code_message.anchor_widget(key) if self._code_message is not None else None

    def set_highlights(self, call_id: str | None, ranges: tuple[tuple[int, int], ...]) -> None:
        if self._reasoning_card is not None:
            self._reasoning_card.set_highlights(ranges)
            return
        if call_id is None:
            highlight_text(self.text, ranges)
        elif self._code_message is not None:
            self._code_message.set_highlights(call_id, ranges)
        else:
            self._tool_group.set_highlights(call_id, ranges)

    def clear_highlights(self) -> None:
        if self._reasoning_card is not None:
            self._reasoning_card.set_highlights(())
            return
        if self._tool_group is not None:
            self._tool_group.clear_highlights()
        elif self._code_message is not None:
            self._code_message.clear_highlights()
        else:
            highlight_text(self.text, ())

    def reveal_match(self, call_id: str | None, start: int, end: int):
        if self._reasoning_card is not None:
            editor = self._reasoning_card.reveal()
            cursor = QTextCursor(editor.document())
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            editor.setTextCursor(cursor)
            return editor, cursor
        if call_id is not None:
            if self._code_message is not None:
                return self._code_message.reveal(call_id, start, end)
            return self._tool_group.reveal(call_id, start, end)
        if self.row.turn_id:
            self._view_state.expanded = True
            self._refresh_fold()
        cursor = QTextCursor(self.text.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self.text.setTextCursor(cursor)
        return self.text, cursor

    def _resize_bubble(self) -> None:
        if self.row is not None and self.row.turn_id:
            self._surface.setFixedWidth(min(self._natural_width, max(60, round(self.width() * 0.88))))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._resize_bubble()

