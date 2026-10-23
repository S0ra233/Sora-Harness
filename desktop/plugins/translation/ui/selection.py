"""双栏只读选区；跨段只记录Task和UTF-16位置，不建立整场景富文本文档。"""
from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QKeySequence, QTextCursor
from PySide6.QtWidgets import QApplication, QTextEdit, QVBoxLayout

from ....ui.common.buttons import MotionButton
from ....ui.common.popup import CardPopup
from ....ui.shell import theme


def reading_text(text):
    """与纯文本文档的段落位置一致；只用于显示和复制，项目数据保持原样。"""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\u2029", "\n").replace("\u2028", "\n")


def _units(text):
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


class ColumnSelection(QObject):
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.column = 0
        self.anchor = self.caret = None
        self.dragging = self._painting = False
        self._menu = None
        self._scroll = QTimer(self)
        self._scroll.setInterval(20)
        self._scroll.timeout.connect(self._drag_scroll)

    def _text(self, key):
        pair = self.view._pairs[key]
        return reading_text(pair.source if self.column == 0 else pair.translation)

    def _ordered(self):
        positions = self.view._geometry.positions
        if self.anchor is None or self.caret is None or self.anchor[0] not in positions or self.caret[0] not in positions:
            return None
        a, b = self.anchor, self.caret
        return (a, b) if (positions[a[0]], a[1]) <= (positions[b[0]], b[1]) else (b, a)

    def bounds(self, key, length):
        ordered = self._ordered()
        if ordered is None or key not in self.view._geometry.positions:
            return None
        first, last = ordered
        positions = self.view._geometry.positions
        if not positions[first[0]] <= positions[key] <= positions[last[0]]:
            return None
        start = first[1] if key == first[0] else 0
        end = last[1] if key == last[0] else length
        return min(start, length), min(end, length)

    def has_selection(self, column):
        ordered = self._ordered()
        return self.column == column and ordered is not None and ordered[0] != ordered[1]

    def paint_editor(self, editor, key, column):
        extra = []
        if column == self.column and not editor.textCursor().hasSelection():
            bounds = self.bounds(key, editor.document().characterCount() - 1)
            if bounds is not None and bounds[0] != bounds[1]:
                selection = QTextEdit.ExtraSelection()
                selection.cursor = QTextCursor(editor.document())
                selection.cursor.setPosition(bounds[0])
                selection.cursor.setPosition(bounds[1], QTextCursor.MoveMode.KeepAnchor)
                selection.format.setBackground(QColor(theme.TEXT_SELECTION))
                # 只叠选区底色，保留文档中姓名与对白各自的颜色。
                extra.append(selection)
        editor.setExtraSelections(extra)

    def paint(self):
        for key, paragraph in self.view._paragraphs.items():
            for column, editor in enumerate(paragraph.editors):
                self.paint_editor(editor, key, column)

    def _clear_native(self, except_editor=None):
        self._painting = True
        try:
            for paragraph in self.view._paragraphs.values():
                for editor in paragraph.editors:
                    if editor is not except_editor and editor.textCursor().hasSelection():
                        cursor = editor.textCursor()
                        cursor.clearSelection()
                        editor.setTextCursor(cursor)
        finally:
            self._painting = False

    def native_changed(self, editor):
        if self._painting:
            return
        cursor = editor.textCursor()
        anchor, caret = (editor.task_id, cursor.anchor()), (editor.task_id, cursor.position())
        if self.column == editor.column and (self.anchor, self.caret) == (anchor, caret):
            return
        self._clear_native(except_editor=editor)
        self.column = editor.column
        self.anchor, self.caret = anchor, caret
        self.paint()

    def begin(self, editor, event, *, word=False):
        self.view.motion.stop()
        editor.setFocus(Qt.FocusReason.MouseFocusReason)
        self._clear_native()
        cursor = editor.cursorForPosition(event.position().toPoint())
        keep_anchor = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier) and self.column == editor.column and self._ordered() is not None
        self.column = editor.column
        if word:
            cursor.select(QTextCursor.SelectionType.WordUnderCursor)
            self.anchor = editor.task_id, cursor.selectionStart()
            self.caret = editor.task_id, cursor.selectionEnd()
        else:
            if not keep_anchor:
                self.anchor = editor.task_id, cursor.position()
            self.caret = editor.task_id, cursor.position()
        # 原生光标只负责后续键盘导航，跨段高亮由逻辑选区投影。
        self._painting = True
        try:
            cursor.clearSelection()
            editor.setTextCursor(cursor)
        finally:
            self._painting = False
        self.dragging = True
        self.paint()

    def extend(self, global_position):
        if not self.dragging or not self.view._geometry.keys:
            return
        viewport = self.view.viewport()
        point = viewport.mapFromGlobal(global_position)
        y = max(0, min(point.y(), viewport.height() - 1))
        for _ in range(2):
            index = self.view._geometry.locate(self.view.verticalScrollBar().value() + y - 16)
            key = self.view._geometry.keys[index]
            anchor = self.view._anchor()
            paragraph, changed = self.view._ensure_paragraph(key)
            if not changed:
                break
            self.view._set_range()
            self.view._restore_anchor(anchor)
            self.view._position_paragraphs()
        editor = paragraph.editors[self.column]
        local = editor.viewport().mapFromGlobal(global_position)
        local.setX(max(0, min(local.x(), editor.viewport().width() - 1)))
        self.caret = key, editor.cursorForPosition(local).position()
        self.paint()
        if point.y() < 0 or point.y() >= viewport.height():
            if not self._scroll.isActive():
                self._scroll.start()
        else:
            self._scroll.stop()

    def _drag_scroll(self):
        if not self.dragging or not self.view.isVisible() or not QApplication.mouseButtons() & Qt.MouseButton.LeftButton:
            self.finish()
            return
        point = self.view.viewport().mapFromGlobal(QCursor.pos())
        distance = point.y() if point.y() < 0 else max(0, point.y() - self.view.viewport().height() + 1)
        if distance:
            bar = self.view.verticalScrollBar()
            bar.setValue(bar.value() + (1 if distance > 0 else -1) * min(48, max(4, abs(distance) // 3)))
        self.extend(QCursor.pos())

    def finish(self):
        self.dragging = False
        self._scroll.stop()

    def hide(self):
        self.finish()
        if self._menu is not None:
            self._menu.hide()

    def reconcile(self, incoming, preserve):
        ordered = self._ordered()
        if ordered is None:
            return
        keys = self.view._geometry.keys
        positions = self.view._geometry.positions
        selected = keys[positions[ordered[0][0]]:positions[ordered[1][0]] + 1]
        field = "source" if self.column == 0 else "translation"
        unchanged = preserve and tuple(incoming) == keys and all(key in incoming and
            getattr(incoming[key], field) == getattr(self.view._pairs[key], field) for key in selected)
        if not unchanged:
            self.hide()
            self.anchor = self.caret = None
            self._clear_native()

    def select_all(self, column):
        self._clear_native()
        self.column = column
        keys = self.view._geometry.keys
        if keys:
            self.anchor, self.caret = (keys[0], 0), (keys[-1], _units(self._text(keys[-1])))
        self.paint()

    def copy(self):
        ordered = self._ordered()
        if ordered is None or ordered[0] == ordered[1]:
            return
        first, last = ordered
        geometry = self.view._geometry
        fragments = []
        for key in geometry.keys[geometry.positions[first[0]]:geometry.positions[last[0]] + 1]:
            text = self._text(key)
            if key in {first[0], last[0]}:
                raw = text.encode("utf-16-le", errors="surrogatepass")
                start = first[1] * 2 if key == first[0] else 0
                end = last[1] * 2 if key == last[0] else len(raw)
                text = raw[start:end].decode("utf-16-le", errors="surrogatepass")
            fragments.append(text)
        QApplication.clipboard().setText("\n".join(fragments))

    def shortcut(self, event, column):
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy()
        elif event.matches(QKeySequence.StandardKey.SelectAll):
            self.select_all(column)
        else:
            return False
        event.accept()
        return True

    def context_menu(self, position, column):
        if self._menu is None:
            self._menu = CardPopup(self.view, "translationSelectionMenu")
            layout = QVBoxLayout(self._menu)
            layout.setContentsMargins(12, 12, 12, 12)
            self._copy_button = MotionButton("复制", self._menu, variant="ghost", size="medium")
            all_button = MotionButton("全选这一列", self._menu, variant="ghost", size="medium")
            self._copy_button.clicked.connect(lambda: (self.copy(), self._menu.dismiss(restore_focus=True)))
            all_button.clicked.connect(lambda: (self.select_all(self._menu_column), self._menu.dismiss(restore_focus=True)))
            layout.addWidget(self._copy_button)
            layout.addWidget(all_button)
        self._menu_column = column
        self._copy_button.setEnabled(self.has_selection(column))
        self._menu.adjustSize()
        available = self.view.screen().availableGeometry()
        self._menu.move(max(available.left(), min(position.x(), available.right() + 1 - self._menu.width())),
                        max(available.top(), min(position.y(), available.bottom() + 1 - self._menu.height())))
        self._menu.present()
