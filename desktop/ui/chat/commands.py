"""应用命令与插件页面共用候选和执行检查；不授予模型工具权限。"""
import re
from dataclasses import dataclass
from collections.abc import Callable
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, Slot
from PySide6.QtGui import QColor, QCursor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QStyle, QStyledItemDelegate, QVBoxLayout

from ..common.icons import icon
from ..common.popup import CardPopup
from ..common.scrolling import ScrollMotion
from ..shell import theme


@dataclass(frozen=True)
class LocalCommand:
    command_id: str
    title: str
    aliases: tuple[str, ...]
    description: str
    icon_name: str
    execute: Callable
    unavailable: Callable[[], str | None]
    whole_input: bool = False
    show_unavailable: bool = False
    # 技能选择只插入调用标记；不在本地菜单直接发送模型请求。
    insert_text: str | None = None
    group: str = "指令"
    source: str = ""


class _CommandDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(220, 28 if index.data(Qt.ItemDataRole.UserRole) is None else 40)

    def paint(self, painter, option, index):
        data = index.data(Qt.ItemDataRole.UserRole)
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = option.rect.adjusted(2, 2, -2, -2)
            if data is None:
                font = QFont(option.font)
                font.setPixelSize(12)
                painter.setFont(font)
                painter.setPen(QColor(theme.FAINT))
                painter.drawText(rect.adjusted(12, 0, -12, 0), Qt.AlignmentFlag.AlignVCenter,
                                 index.data(Qt.ItemDataRole.DisplayRole))
                return
            command, reason = data
            if option.state & (QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_MouseOver):
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(theme.HOVER))
                painter.drawRoundedRect(rect, 10, 10)
            foreground = theme.FAINT if reason else theme.TEXT
            icon(command.icon_name, foreground, 17).paint(painter, QRect(rect.left() + 12, rect.center().y() - 8, 17, 17))
            font = QFont(option.font)
            font.setPixelSize(13)
            painter.setFont(font)
            metrics = QFontMetrics(font)
            source_font = QFont(font)
            source_font.setPixelSize(12)
            source_metrics = QFontMetrics(source_font)
            source = source_metrics.elidedText(command.source, Qt.TextElideMode.ElideRight, min(120, rect.width() // 4))
            source_width = source_metrics.horizontalAdvance(source) + 16 if source else 0
            label = metrics.elidedText(command.title, Qt.TextElideMode.ElideRight, max(0, rect.width() - 52 - source_width))
            label_rect = rect.adjusted(40, 0, -12 - source_width, 0)
            painter.setPen(QColor(foreground))
            painter.drawText(label_rect, Qt.AlignmentFlag.AlignVCenter, label)
            description_rect = label_rect.adjusted(metrics.horizontalAdvance(label) + 10, 0, 0, 0)
            font.setPixelSize(12)
            painter.setFont(font)
            painter.setPen(QColor(theme.FAINT if not reason else theme.MUTED))
            description = QFontMetrics(font).elidedText(reason or command.description, Qt.TextElideMode.ElideRight,
                                                       max(0, description_rect.width()))
            painter.drawText(description_rect, Qt.AlignmentFlag.AlignVCenter, description)
            if source:
                painter.setFont(source_font)
                painter.setPen(QColor(theme.FAINT))
                painter.drawText(rect.adjusted(rect.width() - source_width, 0, -12, 0),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, source)
        finally:
            painter.restore()


class PageCommands(QObject):
    def __init__(self, composer, host, on_error, *, commands=(), command_source=None):
        super().__init__(composer)
        self.composer, self.editor, self.host, self.on_error = composer, composer.editor, host, on_error
        self.commands = tuple(commands)
        self.command_source = command_source
        self._dynamic_commands = ()
        self._source_key = None
        self._source_request = None
        self._picked_draft = None
        if command_source is not None:
            command_source.ready.connect(self._source_ready)
        self.popup = CardPopup(self.editor, "pluginCommands")
        self.popup.radius = 22
        self.popup.setWindowFlags(Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QVBoxLayout(self.popup)
        layout.setContentsMargins(12, 12, 12, 12)
        self.list = QListWidget()
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.popup.setStyleSheet("QFrame#pluginCommands { background: transparent; border: none; }")
        self.list.setStyleSheet("QListWidget { border: none; background: white; outline: none; } QListWidget::item { background: transparent; border: none; }")
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setMouseTracking(True)
        self.list.setUniformItemSizes(False)
        self.list.setItemDelegate(_CommandDelegate(self.list))
        self.list.motion = ScrollMotion(self.list)
        layout.addWidget(self.list)
        self._selectable_rows = []
        self._content_height = 24
        # 在鼠标按下时取条目，避免输入框失焦先关闭弹层、释放时已无候选。
        self.list.viewport().installEventFilter(self)
        self.editor.installEventFilter(composer)
        self.editor.textChanged.connect(self.track)
        self.editor.cursorPositionChanged.connect(self.track)
        self.editor.focus_changed.connect(self._focus_changed)
        self.composer.installEventFilter(self)
        self.popup.closed.connect(self._clear_source)

    def _clear_source(self):
        if self.command_source is not None and self._source_request is not None:
            self.command_source.cancel(self._source_request)
        self._source_key, self._source_request = None, None
        self._dynamic_commands = ()

    def _dismiss(self):
        self._clear_source()
        self.popup.dismiss()

    def _focus_changed(self, focused):
        if focused:
            self.track()
        elif not (self.popup.is_open and self.popup.geometry().contains(QCursor.pos())):
            # 鼠标进入候选卡片时的失焦不撤掉候选；按下选项后仍由_pick统一关闭。
            self._dismiss()

    def eventFilter(self, watched, event):
        if watched is self.list.viewport() and event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            item = self.list.itemAt(event.position().toPoint())
            if item is not None and item.data(Qt.ItemDataRole.UserRole) is not None:
                self.list.setCurrentItem(item)
                self._pick()
                return True
        if watched is self.composer and event.type() == QEvent.Type.Hide:
            self._dismiss()
        return super().eventFilter(watched, event)

    def invalidate_source(self):
        """设置保存或工作区改变时失效；迟到结果不能重开隐藏菜单。"""
        self._clear_source()
        if self.editor.isVisible() and self.editor.hasFocus():
            self.track()
        else:
            self.popup.dismiss()

    def context_changed(self):
        if self.command_source is not None and self._source_key is not None and self._source_key != self.command_source.key(self.host.context().workspace):
            self.invalidate_source()

    @Slot(object)
    def _source_ready(self, result):
        if (result.request_id != self._source_request or result.key != self._source_key
                or result.key != self.command_source.key(self.host.context().workspace)
                or not self.editor.isVisible() or not self.editor.hasFocus()):
            return
        self._source_request = None
        self._dynamic_commands = self.command_source.definitions(result)
        if result.error:
            self.on_error(result.error)
        elif result.issues:
            self.on_error("技能目录部分不可用：" + "；".join(result.issues[:3]))
        self.track()

    def _definitions(self):
        pages = tuple(LocalCommand("page:" + item.page_id, item.title, item.aliases,
                                   "打开辅助内容页面", "plugin",
                                   lambda page=item: self.host.open_page(page.page_id),
                                   lambda page=item: self.host.available(page, self.host.context()), group="插件")
                      for item in self.host.registry.pages())
        return (*self.commands, *pages, *self._dynamic_commands)

    def exact(self):
        text = self.editor.toPlainText()
        match = re.match(r"^/([^\s/]+)(?=\s|$)", text)
        if match is None:
            return False
        definition = next((item for item in self._definitions() if match.group(1).casefold() in
                           [alias.casefold() for alias in item.aliases]), None)
        if definition is None:
            return False
        if definition.insert_text is not None:
            return False
        self._open(definition, match.end())
        return True

    def track(self):
        text = self.editor.toPlainText()
        if text == self._picked_draft:
            return
        self._picked_draft = None
        match = re.match(r"^/([^\s/]*)(?=\s|$)", text)
        cursor = self.editor.textCursor().position()
        if not match or cursor > match.end() or self.editor._preedit or not self.editor.isVisible() or not self.editor.hasFocus():
            self._dismiss()
            return
        query = match.group(1).casefold()
        try:
            if self.command_source is not None:
                # 一个菜单周期仅派发一次后台发现；输入变化只过滤现有元数据。
                key = self.command_source.key(self.host.context().workspace)
                if key != self._source_key:
                    self._clear_source()
                    self._source_key = key
                    self._source_request = self.command_source.request(key)
            matches = [(item, item.unavailable()) for item in self._definitions()
                       if any(query in alias.casefold() for alias in item.aliases) or query in item.title.casefold()]
            visible_matches = [(item, reason) for item, reason in matches if reason is None or item.show_unavailable]
        except (OSError, ValueError) as exc:
            self.on_error(str(exc))
            self._dismiss()
            return
        selected = self.list.currentItem()
        selected_data = selected.data(Qt.ItemDataRole.UserRole) if selected is not None else None
        selected_id = selected_data[0].command_id if selected_data is not None else None
        self.list.clear()
        self._selectable_rows = []
        selected_row = None
        groups, self._content_height = set(), 24
        for item, reason in visible_matches:
            if item.group != "指令" and item.group not in groups:
                heading = QListWidgetItem(item.group, self.list)
                heading.setFlags(Qt.ItemFlag.NoItemFlags)
                self._content_height += 28
                groups.add(item.group)
            self._selectable_rows.append(self.list.count())
            if item.command_id == selected_id:
                selected_row = self.list.count()
            row = QListWidgetItem(item.title, self.list)
            row.setData(Qt.ItemDataRole.UserRole, (item, reason))
            row.setToolTip("/" + item.aliases[0] + " · " + (reason or item.description))
            self._content_height += 40
        if self._source_request is not None:
            if "技能" not in groups:
                heading = QListWidgetItem("技能", self.list)
                heading.setFlags(Qt.ItemFlag.NoItemFlags)
                self._content_height += 28
            notice = QListWidgetItem("正在读取技能目录…", self.list)
            notice.setFlags(Qt.ItemFlag.NoItemFlags)
            self._content_height += 28
        if not visible_matches and self._source_request is None:
            self.popup.dismiss()
            return
        if self._selectable_rows:
            self.list.setCurrentRow(selected_row if selected_row is not None else self._selectable_rows[0])
        self.position_menu()
        self.popup.present()

    def position_menu(self):
        area = self.composer.screen().availableGeometry()
        position = self.composer.mapToGlobal(QPoint(-6, 0))
        below = self.composer.mapToGlobal(QPoint(-6, self.composer.height() + 6))
        above_space, below_space = max(0, position.y() - area.top() - 6), max(0, area.bottom() - below.y() + 1)
        above = above_space >= below_space
        height = min(360, self._content_height, max(24, above_space if above else below_space), area.height())
        width = min(self.composer.width() + 12, area.width())
        self.popup.resize(width, height)
        x = max(area.left(), min(position.x(), area.right() + 1 - width))
        y = position.y() - height - 6 if above else below.y()
        self.popup.move(x, max(area.top(), min(y, area.bottom() + 1 - height)))

    def key(self, event):
        if self.editor._preedit or event.modifiers() != Qt.KeyboardModifier.NoModifier:
            return False
        if self.popup.is_open:
            if event.key() in {Qt.Key.Key_Up, Qt.Key.Key_Down}:
                direction = -1 if event.key() == Qt.Key.Key_Up else 1
                if self._selectable_rows:
                    current = self._selectable_rows.index(self.list.currentRow()) if self.list.currentRow() in self._selectable_rows else 0
                    self.list.setCurrentRow(self._selectable_rows[(current + direction) % len(self._selectable_rows)])
                return True
            if event.key() in {Qt.Key.Key_Home, Qt.Key.Key_End}:
                if self._selectable_rows:
                    self.list.setCurrentRow(self._selectable_rows[0 if event.key() == Qt.Key.Key_Home else -1])
                return True
            if event.key() == Qt.Key.Key_Escape:
                self._dismiss()
                return True
            if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
                if not self._selectable_rows:
                    return self.exact()
                self._pick()
                return True
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            return self.exact()
        return False

    def _pick(self):
        if not self.popup.is_open:
            return
        row = self.list.currentItem()
        data = row.data(Qt.ItemDataRole.UserRole) if row is not None else None
        match = re.match(r"^/[^\s/]*(?=\s|$)", self.editor.toPlainText())
        if match and data is not None:
            definition = data[0]
            inserted = definition.insert_text or "/" + definition.aliases[0]
            # 先固定完成后的草稿，textChanged／焦点通知不能立刻重开同一菜单。
            self._picked_draft = inserted + self.editor.toPlainText()[match.end():]
            self._dismiss()
            cursor = self.editor.textCursor()
            cursor.setPosition(0)
            cursor.setPosition(match.end(), cursor.MoveMode.KeepAnchor)
            cursor.insertText(inserted)
            self.editor.setTextCursor(cursor)
            self.editor.setFocus()

    def _open(self, definition, end):
        try:
            if definition.whole_input and self.editor.toPlainText()[end:].strip():
                raise ValueError("/" + definition.aliases[0] + " 请单独输入；其他草稿内容已保留。")
            reason = definition.unavailable()
            if reason:
                raise ValueError(reason)
            definition.execute()
        except (OSError, ValueError) as exc:
            self.on_error(str(exc))
            return
        self.popup.hide()
        if definition.whole_input:
            end = len(self.editor.toPlainText())
        cursor = self.editor.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(end, cursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        self.editor.setTextCursor(cursor)
