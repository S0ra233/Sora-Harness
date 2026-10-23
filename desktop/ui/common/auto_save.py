"""表单按真实内容判定变化；失焦保存，有效配置与未完成草稿分开。"""
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL

from PySide6.QtCore import QEvent, QObject, QTimer, Slot
from PySide6.QtWidgets import QApplication, QAbstractButton, QComboBox, QLineEdit, QPlainTextEdit, QSpinBox, QWidget

from ...credentials import windows
from ...storage.atomic import write_text_atomic


class FormAutoSave(QObject):
    def __init__(self, owner, fields, commit, path, *, dirty=None, status=None,
                 secrets=(), extra=None, restore_extra=None, ready=lambda: True):
        super().__init__(owner)
        self.owner, self.fields, self.commit = owner, fields, commit
        self.path = Path(path)
        self.dirty, self.status, self.secrets = dirty, status, frozenset(secrets)
        self.extra, self.restore_extra, self.ready = extra, restore_extra, ready
        self.base, self._stored, self._paused, self._failure = None, None, False, None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.flush)
        for widget in fields.values():
            if isinstance(widget, (QLineEdit, QPlainTextEdit)):
                widget.textChanged.connect(self.changed)
            elif isinstance(widget, QComboBox):
                widget.currentIndexChanged.connect(self._choice)
            elif isinstance(widget, QSpinBox):
                widget.valueChanged.connect(self._choice)
            elif isinstance(widget, QAbstractButton):
                widget.toggled.connect(self._choice)
        QApplication.instance().focusChanged.connect(self._focus)
        owner.installEventFilter(self)

    def snapshot(self):
        values = {}
        for name, widget in self.fields.items():
            if isinstance(widget, QLineEdit):
                value = widget.text()
            elif isinstance(widget, QPlainTextEdit):
                value = widget.toPlainText()
            elif isinstance(widget, QComboBox):
                value = widget.currentData()
            elif isinstance(widget, QSpinBox):
                value = widget.value()
            else:
                value = widget.isChecked()
            values[name] = value
        if self.extra is not None:
            values["extra"] = self.extra()
        return values

    def reset(self, *, restore=True):
        self.timer.stop()
        self.base = deepcopy(self.snapshot())
        self._stored, self._failure = None, None
        if not restore and self.path.exists():
            try:
                self._write_draft(self.base, False)
                self._stored = None
            except (OSError, ValueError):
                self._failure = "草稿状态未保存；可点击保存重试。"
                self._note(self._failure)
        if restore and self.path.exists():
            try:
                document = json.loads(self.path.read_text(encoding="utf-8"))
                if document.get("version") != 1:
                    raise ValueError("表单草稿版本不正确。")
                if document.get("active"):
                    values = document["values"]
                    for name in self.secrets:
                        values[name] = ""
                    for name, reference in document.get("secrets", {}).items():
                        if name in self.secrets:
                            values[name] = windows.read(reference)
                    self._restore(values)
                    self._stored = None
                    self._note("已恢复上次填写草稿；完整内容会在离开输入框后保存。")
            except (OSError, ValueError, TypeError, KeyError):
                self._failure = "表单草稿未读取，原文件保留；请检查草稿后重新打开页面。"
                self._note(self._failure)
        self.changed()

    def _restore(self, values):
        self._paused = True
        try:
            for name, widget in self.fields.items():
                if name not in values:
                    continue
                value = values[name]
                if isinstance(widget, QLineEdit):
                    widget.setText(value)
                elif isinstance(widget, QPlainTextEdit):
                    widget.setPlainText(value)
                elif isinstance(widget, QComboBox):
                    index = widget.findData(value)
                    if index < 0 and value is not None:
                        raise ValueError("草稿选项已不可用。")
                    widget.setCurrentIndex(index)
                elif isinstance(widget, QSpinBox):
                    widget.setValue(value)
                else:
                    widget.setChecked(value)
            if self.restore_extra is not None and "extra" in values:
                self.restore_extra(values["extra"])
        finally:
            self._paused = False

    def changed(self, *_):
        if self._paused or self.base is None or not self.ready():
            return
        changed = self.snapshot() != self.base
        if self.dirty is not None:
            self.dirty(changed)
        if not changed:
            self.timer.stop()

    def _choice(self, *_):
        self.changed()
        if not self._paused and self.base is not None and self.ready():
            self.timer.start(0)

    @Slot(QWidget, QWidget)
    def _focus(self, old, new):
        if old is not None and (old is self.owner or self.owner.isAncestorOf(old)) and not self._paused:
            self.timer.start(0)

    def eventFilter(self, watched, event):
        if watched is self.owner and event.type() == QEvent.Type.Hide:
            self.flush()
        return False

    def _write_draft(self, values, active):
        stored, references = deepcopy(values), {}
        for name in self.secrets:
            value = stored.pop(name, "")
            if value:
                reference = uuid5(NAMESPACE_URL, str(self.path.resolve()) + ":" + name).hex
                windows.save(reference, value)
                references[name] = reference
        write_text_atomic(self.path, json.dumps({"version": 1, "active": active, "values": stored,
                                                "secrets": references}, ensure_ascii=False, indent=2))
        self._stored = deepcopy(values)

    def flush(self):
        self.timer.stop()
        if self._paused or self.base is None or not self.ready() or self._failure:
            return self._failure is None
        values = self.snapshot()
        self.changed()
        if values == self.base:
            if self._stored is not None:
                try:
                    self._write_draft(values, False)
                    self._stored = None
                    self._note("改动已恢复为当前保存的内容。")
                except (OSError, ValueError):
                    self._failure = "草稿状态未保存；当前内容保留。"
                    self._note(self._failure)
            return self._failure is None
        if values == self._stored:
            return True
        try:
            # 先留下可恢复输入；正式保存验证失败时仍允许正常离开。
            self._write_draft(values, True)
            self._paused = True
            try:
                accepted = self.commit()
            finally:
                self._paused = False
            if accepted:
                self.base = deepcopy(self.snapshot())
                self._write_draft(self.base, False)
                self._stored = None
                self.changed()
            else:
                previous = self.status.text() if self.status is not None else ""
                self._note(previous + "\n填写草稿已保存；当前有效配置保持原值。")
        except (OSError, ValueError, TypeError):
            self._failure = "自动保存失败；当前输入和原配置保留，可点击保存重试。"
            self._note(self._failure)
        return self._failure is None

    def retry(self):
        self._failure = None
        self._stored = None
        return self.flush()

    def _note(self, message):
        if self.status is not None:
            self.status.setText(message)

    def close_block(self):
        self.flush()
        if self._failure:
            from ..contracts import CloseBlock
            return CloseBlock("自动保存未完成", self._failure)
        return None
