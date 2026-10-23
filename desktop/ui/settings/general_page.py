"""通用设置采用居中的白色页面、圆角卡片与共用下拉浮层。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ...application.general_settings import GeneralOptions
from ..common.buttons import MotionButton
from ..common.surfaces import CardSurface, WhiteSurface, settings_column
from ..common.widgets import ClickComboBox


class GeneralPage(WhiteSurface):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service, self._loading = service, True
        layout = settings_column(self)
        heading = QLabel("通用")
        heading.setObjectName("settingsHeading")
        layout.addWidget(heading)
        content = QWidget()
        row = QHBoxLayout(content)
        row.setContentsMargins(8, 8, 8, 8)
        row.setSpacing(24)
        labels = QVBoxLayout()
        name = QLabel("提问频率")
        name.setStyleSheet("font-weight: 600;")
        labels.addWidget(name)
        note = QLabel("决定模型何时主动询问重要信息；不影响思考强度或工具权限。")
        note.setObjectName("settingsNote")
        note.setWordWrap(True)
        labels.addWidget(note)
        row.addLayout(labels, 1)
        self.frequency = ClickComboBox()
        self.frequency.setAccessibleName("提问频率")
        self.frequency.addItem("低 · 必要时询问", "low")
        self.frequency.addItem("中 · 关键取舍", "medium")
        self.frequency.addItem("高 · 共同设计", "high")
        self.frequency.setMinimumWidth(180)
        self.frequency.currentIndexChanged.connect(self._save)
        row.addWidget(self.frequency)
        layout.addWidget(CardSurface(content))
        info = QLabel("问题显示在输入框上方，模型继续不依赖答案的工作。回答会在下一次请求前加入；"
                      "未回答的问题可以留到下一轮。修改频率在下一次独立运行生效。")
        info.setObjectName("settingsNote")
        info.setWordWrap(True)
        layout.addWidget(info)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setObjectName("settingsNote")
        layout.addWidget(self.status)
        reload = MotionButton("重新读取", variant="secondary")
        reload.clicked.connect(self.reload)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(reload)
        layout.addLayout(buttons)
        layout.addStretch()
        self.reload()

    def reload(self):
        self._loading = True
        try:
            options = self.service.load()
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))
            self.frequency.setEnabled(False)
        else:
            self.frequency.setCurrentIndex(self.frequency.findData(options.question_frequency))
            self.frequency.setEnabled(True)
            self.status.clear()
        finally:
            self._loading = False

    def _save(self, *_):
        if self._loading:
            return
        try:
            self.service.save(GeneralOptions(self.frequency.currentData()))
        except (OSError, ValueError) as exc:
            self.reload()
            self.status.setText(f"选择未保存：{exc}")
        else:
            self.status.setText("已保存；下一次独立运行采用新的提问频率。")
