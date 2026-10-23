"""非模态问题卡片停靠输入框上方；题目、草稿与后台请求分别管理。"""
from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from ..common.buttons import MotionButton
from ..common.icons import icon
from ..common.scrolling import SmoothScrollArea
from ..common.surfaces import paint_card
from ..common.widgets import IconGlyph
from ..shell import theme


class QuestionChoice(MotionButton):
    def __init__(self, index, option, parent=None):
        super().__init__(parent, variant="ghost", shape="pill")
        self.setObjectName("questionOption")
        self.setCheckable(True)
        self.setMinimumHeight(36)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setAccessibleName(option.label)
        self.setToolTip(option.description)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 5, 10, 5)
        row.setSpacing(10)
        number = QLabel(str(index + 1))
        number.setFixedSize(22, 22)
        number.setAlignment(Qt.AlignmentFlag.AlignCenter)
        number.setStyleSheet(f"background: {theme.CONTROL}; color: {theme.MUTED}; border: 1px solid {theme.BORDER}; border-radius: 5px; font-size: 12px;")
        label = QLabel(option.label)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        row.addWidget(number)
        row.addWidget(label, 1)
        self.mark = IconGlyph("right", size=14)
        self.mark.setFixedWidth(14)
        row.addWidget(self.mark)
        for widget in (number, label, self.mark):
            widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.toggled.connect(self.mark.setVisible)
        self.mark.hide()

    def sizeHint(self):
        size = self.layout().sizeHint()
        return QSize(max(120, size.width()), max(36, size.height()))

    def heightForWidth(self, width):
        return max(36, self.layout().heightForWidth(width))


class QuestionDeck(QWidget):
    answered = Signal(object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items, self._index, self._owner = (), 0, None
        self._drafts, self._closed, self._skipped = {}, set(), set()
        self._known = {}
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.open_button = MotionButton("问题", variant="ghost")
        self.open_button.setObjectName("cancelButton")
        self.open_button.setIcon(icon("question"))
        self.open_button.clicked.connect(self._open)
        folded = QHBoxLayout()
        folded.addStretch()
        folded.addWidget(self.open_button)
        layout.addLayout(folded)
        self.card = _QuestionSurface()
        body = QVBoxLayout(self.card)
        body.setContentsMargins(18, 14, 18, 14)
        body.setSpacing(12)
        header = QHBoxLayout()
        header.addWidget(IconGlyph("question", size=16))
        title = QLabel("问题")
        title.setStyleSheet(f"color: {theme.MUTED};")
        header.addWidget(title)
        header.addStretch()
        self.previous = self._icon_button("left", "上一题", lambda: self._move(-1))
        self.next = self._icon_button("right", "下一题", lambda: self._move(1))
        self.position = QLabel()
        self.position.setStyleSheet(f"color: {theme.FAINT}; font-size: 12px;")
        header.addWidget(self.previous)
        header.addWidget(self.position)
        header.addWidget(self.next)
        header.addWidget(self._icon_button("close", "收起问题；模型继续工作", self._close))
        body.addLayout(header)
        self.scroll = SmoothScrollArea()
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.content = QWidget()
        self.content.setStyleSheet("background: transparent;")
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(5)
        self.prompt = QLabel()
        self.prompt.setTextFormat(Qt.TextFormat.PlainText)
        self.prompt.setWordWrap(True)
        self.prompt.setStyleSheet("font-weight: 600; padding: 5px 2px;")
        self.content_layout.addWidget(self.prompt)
        self.scroll.setWidget(self.content)
        body.addWidget(self.scroll)
        bottom = QHBoxLayout()
        bottom.setSpacing(10)
        bottom.addWidget(IconGlyph("edit", size=16))
        self.reply = QLineEdit()
        self.reply.setPlaceholderText("或自行撰写回复")
        self.reply.setAccessibleName("问题回复")
        self.reply.setMaxLength(20000)
        self.reply.setStyleSheet(f"QLineEdit {{ background: transparent; color: {theme.TEXT}; border: none; padding: 5px 0; selection-background-color: {theme.TEXT_SELECTION}; selection-color: {theme.TEXT}; }}")
        self.reply.textChanged.connect(self._draft_changed)
        self.reply.returnPressed.connect(self._submit)
        bottom.addWidget(self.reply, 1)
        self.skip = MotionButton("跳过", variant="secondary", shape="pill")
        self.skip.setObjectName("questionSkip")
        self.skip.setFixedHeight(32)
        self.skip.setStyleSheet("padding: 0 12px;")
        self.skip.clicked.connect(self._skip)
        self.send = MotionButton("发送", variant="primary", shape="pill")
        self.send.setObjectName("questionSend")
        self.send.setFixedHeight(32)
        self.send.setStyleSheet("padding: 0 16px;")
        self.send.clicked.connect(self._submit)
        bottom.addWidget(self.skip)
        bottom.addWidget(self.send)
        body.addLayout(bottom)
        layout.addWidget(self.card)
        escape = QShortcut(QKeySequence("Esc"), self)
        escape.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        escape.activated.connect(self._close)
        self.hide()

    def _icon_button(self, name, tooltip, callback):
        button = MotionButton(variant="ghost")
        button.setObjectName("cancelButton")
        button.setIcon(icon(name, size=14))
        button.setIconSize(QSize(14, 14))
        button.setFixedSize(26, 26)
        button.setStyleSheet("padding: 0;")
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.clicked.connect(callback)
        return button

    def set_questions(self, owner, items):
        old = self.current
        old_keys = self._known.get(owner, set())
        old_answer = old.answer if old is not None else None
        old_key = old.key if old is not None and owner == self._owner else None
        self._owner, self._items = owner, tuple(items)
        self._known[owner] = {item.key for item in self._items}
        if not self._items:
            self.hide()
            return
        # 刷新同一题只更新接纳状态，不重建选项或打断正在输入的文字。
        self._index = next((i for i, item in enumerate(self._items) if item.key == old_key),
                           self._first_unanswered())
        newly_answered = old_key is not None and old_answer is None and self.current.answer is not None
        if newly_answered:
            self._index = self._first_unanswered()
        new_questions = any(item.key not in old_keys for item in self._items)
        if new_questions and owner in self._closed:
            self._index = self._first_unanswered()
        if new_questions:
            self._closed.discard(owner)
        if (newly_answered or old_key is None) and all(item.answer is not None for item in self._items):
            self._closed.add(owner)
        self.show()
        pending = sum(item.answer is None for item in self._items)
        self.open_button.setText(f"问题 · {pending} 待回答" if pending else f"问题 · {len(self._items)} 已回答")
        self.card.setVisible(owner not in self._closed)
        self.open_button.setVisible(owner in self._closed)
        if old != self.current or old_key is None:
            self._show_current()
        else:
            self.position.setText(f"{self._index + 1} / {len(self._items)}")
            self.previous.setEnabled(self._index > 0)
            self.next.setEnabled(self._index + 1 < len(self._items))

    @property
    def current(self):
        return self._items[self._index] if self._items else None

    def _first_unanswered(self):
        return next((i for i, item in enumerate(self._items) if item.answer is None and not item.sending),
                    min(self._index, max(0, len(self._items) - 1)))

    def _show_current(self):
        item = self.current
        if item is None:
            return
        self._loading = True
        self.prompt.setText(item.question.question)
        self.position.setText(f"{self._index + 1} / {len(self._items)}")
        self.previous.setEnabled(self._index > 0)
        self.next.setEnabled(self._index + 1 < len(self._items))
        while self.content_layout.count() > 1:
            widget = self.content_layout.takeAt(1).widget()
            widget.hide()
            widget.deleteLater()
        selected, text = self._drafts.get((self._owner, item.key), (None, ""))
        self._choices = []
        for index, option in enumerate(item.question.options):
            choice = QuestionChoice(index, option)
            choice.setChecked(selected == index)
            choice.setEnabled(item.answer is None and not item.sending)
            choice.clicked.connect(lambda checked=False, index=index: self._choose(index))
            self.content_layout.addWidget(choice)
            self._choices.append(choice)
        self.reply.setText(item.answer if item.answer is not None else text)
        self.reply.setReadOnly(item.answer is not None or item.sending)
        self.skip.setEnabled(item.answer is None and not item.sending)
        self.send.setText("已回答" if item.answer is not None else "下一步" if self._index + 1 < len(self._items) else "发送")
        # 长题目使用有界滚动，问题卡片不能占满整个聊天窗口。
        self._size_content()
        self._loading = False
        self._update_send()

    def _choose(self, index):
        item = self.current
        _, text = self._drafts.get((self._owner, item.key), (None, ""))
        self._drafts[self._owner, item.key] = index, text
        for i, choice in enumerate(self._choices):
            choice.setChecked(i == index)
        self._update_send()

    def _draft_changed(self, text):
        if self._loading or self.current is None:
            return
        key = self._owner, self.current.key
        selected, _ = self._drafts.get(key, (None, ""))
        self._drafts[key] = selected, text
        self._update_send()

    def _update_send(self):
        item = self.current
        selected, text = self._drafts.get((self._owner, item.key), (None, ""))
        self.send.setEnabled(item.answer is None and not item.sending and (selected is not None or bool(text.strip())))

    def _submit(self):
        item = self.current
        if item is None or not self.send.isEnabled():
            return
        selected, text = self._drafts.get((self._owner, item.key), (None, ""))
        answer = item.question.options[selected].label if selected is not None else ""
        if text.strip():
            answer = f"{answer}\n\n{text.strip()}".strip()
        self.answered.emit(item.key, answer)

    def _move(self, delta):
        if self._items and 0 <= self._index + delta < len(self._items):
            self._index += delta
            self._show_current()

    def _skip(self):
        self._skipped.add((self._owner, self.current.key))
        next_index = next((i for i, item in enumerate(self._items) if item.answer is None
                           and not item.sending and (self._owner, item.key) not in self._skipped), None)
        if next_index is None:
            self._close()
        else:
            self._index = next_index
            self._show_current()

    def _close(self):
        self._closed.add(self._owner)
        self.card.hide()
        self.open_button.show()

    def _open(self):
        self._closed.discard(self._owner)
        self._index = self._first_unanswered()
        self.card.show()
        self.open_button.hide()
        self._show_current()

    def _size_content(self):
        height = self.content.heightForWidth(max(100, self.width() - 36))
        if height < 0:
            height = self.content.sizeHint().height()
        self.scroll.setFixedHeight(min(190, max(70, height)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.current is not None:
            self._size_content()


class _QuestionSurface(QWidget):
    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            paint_card(painter, QRectF(self.rect()).adjusted(1, 1, -1, -1), radius=22, shadow=0)
        finally:
            painter.end()
