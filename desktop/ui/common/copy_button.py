"""复制完整来源文本；反馈和计时只属于按钮。"""
from PySide6.QtCore import QSize, QTimer
from PySide6.QtGui import QGuiApplication

from .icons import icon
from .widgets import IconButton
from ..shell import theme


class CopyButton(IconButton):
    def __init__(self, source, parent=None) -> None:
        super().__init__("copy", "复制", parent, size=24)
        self.setIconSize(QSize(14, 14))
        self._source = source
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self._reset)
        self.clicked.connect(self._copy)

    def _copy(self) -> None:
        text = self._source()
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            self.setToolTip("剪贴板暂不可用")
            return
        clipboard.setText(text)
        if clipboard.text() != text:
            self.setToolTip("未能确认剪贴板内容，请重试")
            return
        self.setIcon(icon("check", theme.SUCCESS))
        self.setToolTip("已复制")
        self.setAccessibleName("已复制")
        self._timer.start()

    def _reset(self) -> None:
        self.setIcon(icon("copy"))
        self.setToolTip("复制")
        self.setAccessibleName("复制")
