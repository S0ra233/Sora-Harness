"""草稿与图片输入；控件发出意图，保存、请求与取消由控制器执行。"""
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import (
    QFileDialog, QFrame, QHBoxLayout, QPlainTextEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from ...attachments.store import ImageStore
from ...llm.messages import ImageContent
from ...llm.models import ModelConfig
from ...runtime.conversations import ConversationSettings, TurnOptions
from ..settings.model_picker import SavedModelButton
from ..common.icons import icon
from ..common.widgets import ElidedLabel, IconButton, IconGlyph
from .context import ContextMeter
from ..shell import theme
from ..common.buttons import MotionButton


class PromptEditor(QPlainTextEdit):
    send_requested = Signal()
    focus_changed = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._preedit = False
        self._resizing = False
        self.can_send = False
        self._hint = "随心输入"
        self.setPlaceholderText(self._hint)
        font = self.font()
        font.setFamilies(theme.TEXT_FAMILIES)
        font.setPixelSize(theme.BODY_FONT_SIZE)
        self.setFont(font)
        self.document().setDocumentMargin(2)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(theme.FAINT))
        self.setPalette(palette)
        self.setFixedHeight(42)
        self.textChanged.connect(self._resize_input)
        self.textChanged.connect(self._sync_placeholder)
        self.document().documentLayout().documentSizeChanged.connect(self._resize_input)

    def inputMethodEvent(self, event) -> None:
        self._preedit = bool(event.preeditString())
        # 拼音组合文字尚未进入文档，不能只依赖textChanged判断占位是否可见。
        self._sync_placeholder()
        super().inputMethodEvent(event)
        self._sync_placeholder()

    def _sync_placeholder(self) -> None:
        hint = self._hint if not self._preedit and self.document().isEmpty() else ""
        if self.placeholderText() != hint:
            self.setPlaceholderText(hint)
            self.viewport().update()

    def keyPressEvent(self, event) -> None:
        if (event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter} and event.modifiers() == Qt.KeyboardModifier.NoModifier
                and not self._preedit and self.can_send):
            self.send_requested.emit()
            event.accept()
        else:
            super().keyPressEvent(event)

    def _resize_input(self, *args) -> None:
        if self._resizing:
            return
        self._resizing = True
        try:
            # QTextLayout的行数包含自动折行；只统计到六行便足够决定高度。
            lines, block = 0, self.document().begin()
            while block.isValid() and lines < 6:
                self.document().documentLayout().blockBoundingRect(block)
                lines += max(1, block.layout().lineCount())
                block = block.next()
            height = max(42, min(6, lines) * self.fontMetrics().lineSpacing() + 24)
            if self.height() != height:
                self.setFixedHeight(height)
        finally:
            self._resizing = False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._resize_input()

    def focusInEvent(self, event) -> None:
        super().focusInEvent(event)
        self.focus_changed.emit(True)

    def focusOutEvent(self, event) -> None:
        super().focusOutEvent(event)
        self.focus_changed.emit(False)


class Composer(QFrame):
    draft_changed = Signal(str)
    send_requested = Signal()
    stop_requested = Signal()
    images_requested = Signal(object)
    image_removed = Signal(str)
    effort_changed = Signal(object)
    model_selection_changed = Signal(object)
    queue_requested = Signal()

    def __init__(self, images: ImageStore, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("chatComposer")
        self._images, self._references = images, ()
        self._model_config: ModelConfig | None = None
        self._ready, self._busy, self._save_pending, self._image_supported = False, False, False, False
        self._supplement, self._resumable = False, False
        self.page_commands = None
        self._density = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 10)
        layout.setSpacing(8)
        # 输入区包含独立弹层，不把整个子树放入阴影的离屏绘制。
        self._attachments = QWidget()
        self._attachment_layout = QHBoxLayout(self._attachments)
        self._attachment_layout.setContentsMargins(0, 0, 0, 0)
        self._attachment_layout.addStretch()
        self.editor = PromptEditor()
        controls = QHBoxLayout()
        self._controls = controls
        controls.setSpacing(8)
        self._attach = IconButton("plus", "添加图片")
        self._queue = MotionButton("排队发送", variant="ghost")
        self._queue.setAccessibleName("排队发送")
        self._queue.setStyleSheet(f"QPushButton {{ color: {theme.FAINT}; background: transparent; border: none; padding: 4px 8px; border-radius: 8px; font-size: 11px; }} QPushButton:hover {{ background: #f0f0f0; }}")
        self._queue.setToolTip("当前目标正常完成后，作为独立一轮发送；停止后保留。")
        self._queue.clicked.connect(self._activate_queue)
        self.context = ContextMeter()
        self._model = SavedModelButton()
        self._model.selection_changed.connect(self._selection_changed)
        self._send = MotionButton(variant="accent", shape="pill")
        self._send.setObjectName("chatSend")
        self._send.setFixedSize(32, 32)
        self._send.setIconSize(QSize(20, 20))
        self._send.setCursor(Qt.CursorShape.PointingHandCursor)
        controls.addWidget(self._attach)
        controls.addWidget(self._queue)
        controls.addStretch()
        controls.addWidget(self.context)
        controls.addWidget(self._model)
        controls.addWidget(self._send)
        layout.addWidget(self._attachments)
        layout.addWidget(self.editor)
        layout.addLayout(controls)
        self.editor.textChanged.connect(self._on_text)
        self.editor.send_requested.connect(self._activate)
        self.editor.focus_changed.connect(self._focus_border)
        self._send.clicked.connect(self._activate)
        self._attach.clicked.connect(self._choose_images)
        self._update_enabled()

    def set_settings(self, settings: ConversationSettings | None, options: TurnOptions | None = None) -> None:
        target = options.model_target if options is not None else None
        config = target.config if target is not None else settings.config if settings else None
        self._model_config = config
        self._image_supported = config is not None and config.image_input is True
        effort = options.reasoning_effort if options is not None else settings.reasoning_effort if settings else None
        self._model.set_settings(settings, effort, target)
        self._attach.setToolTip("添加图片" if self._image_supported else "此模型尚未确认支持图片，请选择支持图片的模型。")
        self._update_enabled()

    def set_model_catalog(self, catalog) -> None:
        self._model.set_catalog(catalog)

    def _selection_changed(self) -> None:
        selection = self._model.selection()
        self.model_selection_changed.emit(selection)
        self.effort_changed.emit(selection.effort)

    def restore_effort(self, effort: str | None) -> None:
        self._model.restore_effort(effort)

    def set_model_control(self, widget: QWidget) -> None:
        """新聊天复用输入区，只替换选择控件，不改变已有会话的后台对象。"""
        previous = self._model
        self._controls.replaceWidget(previous, widget)
        previous.hide()
        previous.deleteLater()
        self._model = widget
        self._density = None
        self._fit_controls()

    def set_new_model(self, config: ModelConfig | None) -> None:
        self._model_config = config
        self._image_supported = config is not None and config.image_input is True
        self._attach.setToolTip("添加图片" if self._image_supported else "此模型尚未确认支持图片。")
        self._update_enabled()

    def restore(self, text: str, images: tuple[ImageContent, ...]) -> None:
        if text != self.editor.toPlainText():
            self.editor.blockSignals(True)
            self.editor.setPlainText(text)
            self.editor.blockSignals(False)
            self.editor._sync_placeholder()
            self.editor._resize_input()
        if images != self._references:
            self._references = images
            while self._attachment_layout.count() > 1:
                item = self._attachment_layout.takeAt(0)
                item.widget().deleteLater()
            for reference in images:
                try:
                    name = self._images.metadata(reference).name
                except (OSError, ValueError):
                    name = f"图片 {reference.attachment_id[:8]}"
                chip = QFrame()
                chip.setObjectName("attachmentChip")
                chip.setMaximumWidth(260)
                row = QHBoxLayout(chip)
                row.setContentsMargins(8, 3, 3, 3)
                row.setSpacing(5)
                row.addWidget(IconGlyph("paperclip", size=14))
                label = ElidedLabel(name)
                label.setToolTip(name)
                row.addWidget(label, 1)
                remove = IconButton("close", "从草稿移除引用；本地图片保留", size=22)
                remove.clicked.connect(lambda _checked=False, key=reference.attachment_id: self.image_removed.emit(key))
                row.addWidget(remove)
                self._attachment_layout.insertWidget(self._attachment_layout.count() - 1, chip)
        self._attachments.setVisible(bool(images))
        self._update_enabled()

    def set_state(self, ready: bool, busy: bool, save_pending: bool, *, supplement: bool = False,
                  resumable: bool = False) -> None:
        self._ready, self._busy, self._save_pending = ready, busy, save_pending
        self._supplement, self._resumable = supplement, resumable
        self._update_enabled()
        if self.page_commands is not None and self.page_commands.popup.is_open:
            self.page_commands.track()

    def _update_enabled(self) -> None:
        self.editor.setEnabled(self._ready)
        content = bool(self.editor.toPlainText().strip() or self._references)
        can_send = self._ready and self._model_config is not None and not self._save_pending and (not self._busy or self._supplement) and (content or not self._busy and self._resumable)
        self.editor.can_send = can_send
        stopping = self._busy and (not content or not self._supplement)
        self._send.setIcon(icon("stop" if stopping else "arrow", "#ffffff", 20))
        label = "停止" if stopping else "补充" if self._busy else "继续" if self._resumable else "发送"
        self._send.setToolTip(label + "此会话当前任务" if label in {"停止", "继续"} else label + "消息")
        self._send.setAccessibleName(label)
        self._send.setEnabled(self._busy or can_send)
        if not stopping:
            self._send.setEnabled(can_send)
        self._queue.setVisible(self._ready and self._busy and self._supplement and content)
        self._queue.setEnabled(can_send)
        self._fit_controls()
        self._attach.setEnabled(self._ready and (not self._busy or self._supplement) and not self._save_pending and self._image_supported)

    def _on_text(self) -> None:
        self._update_enabled()
        self.draft_changed.emit(self.editor.toPlainText())

    def _focus_border(self, focused: bool) -> None:
        self.setProperty("focused", focused)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def _activate(self) -> None:
        if self.page_commands is not None and self.page_commands.exact():
            return
        stopping = self._busy and (not self._supplement or not (self.editor.toPlainText().strip() or self._references))
        (self.stop_requested if stopping else self.send_requested).emit()

    def _activate_queue(self) -> None:
        if self.page_commands is None or not self.page_commands.exact():
            self.queue_requested.emit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_controls()
        if self.page_commands is not None and self.page_commands.popup.is_open:
            self.page_commands.position_menu()

    def _fit_controls(self) -> None:
        """分栏缩窄时先收起次要标签，排队与发送操作仍然可用。"""
        if not hasattr(self, "_model"):
            return
        margins = self.layout().contentsMargins()
        available = self.width() - margins.left() - margins.right()
        level = 2 if available < theme.COMPOSER_NARROW_WIDTH else 1 if available < theme.COMPOSER_COMPACT_WIDTH else 0
        if level == self._density:
            return
        self._density = level
        self._controls.setSpacing((8, 6, 4)[level])
        self._model.set_compact(level)
        self._queue.setText("" if level == 2 else "排队发送")
        self._queue.setIcon(icon("right") if level == 2 else QIcon())
        self._queue.setFixedWidth(30 if level == 2 else self._queue.sizeHint().width())

    def eventFilter(self, watched, event):
        if watched is self.editor and event.type() == QEvent.Type.KeyPress and self.page_commands is not None:
            if self.page_commands.key(event):
                event.accept()
                return True
        return super().eventFilter(watched, event)

    def _choose_images(self) -> None:
        paths, _filter = QFileDialog.getOpenFileNames(self, "选择图片", "", "图片 (*.png *.jpg *.jpeg *.webp *.gif)")
        if paths:
            self.images_requested.emit([Path(path) for path in paths])
