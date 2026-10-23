"""代码块的有界词法着色；保留Qt原字符和选择，不生成HTML或重排全文。"""
from threading import Event
from time import perf_counter
import logging
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat
from pygments.lexers import get_lexer_by_name
from pygments.styles import get_style_by_name
from pygments.util import ClassNotFound


class _LexResult(QObject):
    ready = Signal(int, object)


class _LexTask(QRunnable):
    """后台只处理字符串与位置；文档和字体始终属于界面线程。"""
    def __init__(self, source, language, generation, cancelled):
        super().__init__()
        self.source, self.language = source, language
        self.generation, self.cancelled = generation, cancelled
        self.result = _LexResult()

    def run(self):
        blocks = {}
        if self.cancelled.is_set():
            return
        try:
            lexer = get_lexer_by_name(self.language.split()[0] if self.language.strip() else "text")
            previous, line, column = 0, 0, 0
            for start, token, value in lexer.get_tokens_unprocessed(self.source):
                if self.cancelled.is_set():
                    return
                gap = self.source[previous:start]
                if "\n" in gap:
                    line += gap.count("\n")
                    column = len(gap.rsplit("\n", 1)[-1].encode("utf-16-le")) // 2
                else:
                    column += len(gap.encode("utf-16-le")) // 2
                segments = value.split("\n")
                for index, segment in enumerate(segments):
                    length = len(segment.encode("utf-16-le")) // 2
                    if length:
                        blocks.setdefault(line, []).append((column, length, token))
                    column += length
                    if index < len(segments) - 1:
                        line, column = line + 1, 0
                previous = start + len(value)
        except ClassNotFound:
            blocks = {}
        except Exception as exc:
            logging.getLogger(__name__).warning("代码着色降级为原文（%s）", type(exc).__name__)
            blocks = {}
        if not self.cancelled.is_set():
            self.result.ready.emit(self.generation, blocks)


class CodeHighlighter(QSyntaxHighlighter):
    """每个可见代码卡片持有自己的结果；换文档时由Qt关闭，不建立全局消息缓存。"""

    def __init__(self, document) -> None:
        super().__init__(document)
        self._key = None
        self._blocks = {}
        self._formats = {}
        self._style = get_style_by_name("friendly")
        self._generation = 0
        self._cancelled = Event()
        self._paint = QTimer(self)
        self._paint.setSingleShot(True)
        self._paint.timeout.connect(self._paint_batch)
        self._next_block = None
        document.destroyed.connect(self._stop)

    def set_source(self, text: str, language: str, *, settled: bool) -> None:
        # 这里只限制语法着色成本，完整代码仍保留、显示、搜索和复制。
        source = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u2029", "\n") if settled and len(text) <= 256_000 else None
        key = (source, language)
        if key == self._key:
            return
        self._key = key
        self._cancelled.set()
        self._cancelled = Event()
        self._generation += 1
        self._blocks = {}
        self._schedule_paint()
        if source is not None:
            task = _LexTask(source, language, self._generation, self._cancelled)
            task.result.ready.connect(self._receive)
            QThreadPool.globalInstance().start(task)

    def _stop(self, *_args):
        self._cancelled.set()
        self._paint.stop()

    @Slot(int, object)
    def _receive(self, generation, blocks):
        if generation == self._generation and not self._cancelled.is_set():
            self._blocks = blocks
            self._schedule_paint()

    def _schedule_paint(self):
        self._next_block = self.document().firstBlock()
        self._paint.start(0)

    def _paint_batch(self):
        deadline = perf_counter() + .006
        while self._next_block is not None and self._next_block.isValid():
            block = self._next_block
            self._next_block = block.next()
            self.rehighlightBlock(block)
            if perf_counter() >= deadline:
                break
        if self._next_block is not None and self._next_block.isValid():
            self._paint.start(16)
        else:
            self._next_block = None

    def _token_format(self, token) -> QTextCharFormat:
        if token not in self._formats:
            base_token = token
            # 词法器可返回细分类型，样式未列出时沿公开parent链继承，不让高亮中断阅读。
            while not self._style.styles_token(base_token) and base_token.parent is not None:
                base_token = base_token.parent
            appearance = self._style.style_for_token(base_token)
            style = QTextCharFormat()
            if appearance["color"]:
                style.setForeground(QColor("#" + appearance["color"]))
            if appearance["bold"]:
                style.setFontWeight(QFont.Weight.Bold)
            if appearance["italic"]:
                style.setFontItalic(True)
            self._formats[token] = style
        return self._formats[token]

    def highlightBlock(self, text: str) -> None:
        for start, length, token in self._blocks.get(self.currentBlock().blockNumber(), ()):
            self.setFormat(start, length, self._token_format(token))
