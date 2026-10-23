"""只读长文本的阅读事件；正文代码与工具输出共用。"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QTextEdit

from .scrolling import ScrollMotion
from .read_only_paint import draw_read_only_document


class ReadOnlyText(QTextEdit):
    interacted = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptRichText(False)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        font = self.font()
        font.setFamilies(["Consolas", "Courier New"])
        font.setPixelSize(13)
        self.setFont(font)
        self.document().setDefaultFont(font)
        self.document().setDocumentMargin(0)
        self.motion = ScrollMotion(self)
        self.motion.wheel_started.connect(lambda _direction: self.interacted.emit())

    def blockCount(self) -> int:
        """保留两个现有展示调用方的行数接口；滚动位置改为像素。"""
        return self.document().blockCount()

    def paintEvent(self, event) -> None:
        if self.textCursor().hasSelection():
            draw_read_only_document(self, event)
        else:
            super().paintEvent(event)

    def wheelEvent(self, event) -> None:
        self.interacted.emit()
        super().wheelEvent(event)
        # 普通滚轮由ScrollMotion按范围路由；这里只保留Ctrl等原生处理。
        event.accept()

    def mousePressEvent(self, event) -> None:
        self.interacted.emit()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in {Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Left, Qt.Key.Key_Right,
                           Qt.Key.Key_PageUp, Qt.Key.Key_PageDown, Qt.Key.Key_Home, Qt.Key.Key_End}:
            self.interacted.emit()
        super().keyPressEvent(event)
