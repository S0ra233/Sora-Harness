"""收集准备项目的输入并展示检测结果；后台执行由页面管理。"""
from typing import Any

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)


class ProjectPreparePanel(QWidget):
    """发出的字典固定本次输入；不调用 Core，不创建工作目录。"""

    detect_requested = Signal(object)
    prepare_requested = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._detected_game: str | None = None
        self._busy = False
        self._build_form()

    def _build_form(self) -> None:
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._game_input = QLineEdit()
        self._game_input.setPlaceholderText("游戏启动文件所在目录")
        self._work_input = QLineEdit()
        self._work_input.setPlaceholderText("独立工作目录；可以直接填写新目录路径")
        self._game_browse = QPushButton("选择目录")
        self._work_browse = QPushButton("选择目录")
        for title, field, button in (
            ("游戏目录", self._game_input, self._game_browse),
            ("工作目录", self._work_input, self._work_browse),
        ):
            row = QHBoxLayout()
            row.addWidget(field)
            row.addWidget(button)
            form.addRow(title, row)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        self._detect_button = QPushButton("检测游戏")
        self._prepare_button = QPushButton("准备项目")
        buttons.addWidget(self._detect_button)
        buttons.addWidget(self._prepare_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self._detection_label = QLabel("先检测游戏，再准备项目。当前使用默认提取方式。")
        self._detection_label.setWordWrap(True)
        self._detection_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self._detection_label)
        self._game_browse.clicked.connect(self._on_browse_game)
        self._work_browse.clicked.connect(self._on_browse_work)
        self._game_input.textChanged.connect(self._on_game_changed)
        self._work_input.textChanged.connect(self._update_buttons)
        self._detect_button.clicked.connect(self._on_detect)
        self._prepare_button.clicked.connect(self._on_prepare)
        self._update_buttons()

    @Slot()
    def _on_browse_game(self) -> None:
        self._choose_directory(self._game_input, "选择游戏目录")

    @Slot()
    def _on_browse_work(self) -> None:
        self._choose_directory(self._work_input, "选择翻译工作目录")

    def _choose_directory(self, field: QLineEdit, title: str) -> None:
        directory = QFileDialog.getExistingDirectory(self, title, field.text())
        if directory:
            field.setText(directory)

    @Slot(str)
    def _on_game_changed(self, text: str) -> None:
        """换目录后使旧检测失效，不能拿上一款游戏的结果准备当前游戏。"""
        self._detected_game = None
        self._detection_label.setText("游戏目录已更改，请重新检测。")
        self._update_buttons()

    @Slot()
    def _on_detect(self) -> None:
        self._detected_game = None
        self._detection_label.setText("正在检测引擎与资源线索……")
        self.detect_requested.emit({"game_dir": self._game_input.text().strip()})

    @Slot()
    def _on_prepare(self) -> None:
        self.prepare_requested.emit({
            "game_dir": self._game_input.text().strip(),
            "work_dir": self._work_input.text().strip(),
        })

    def show_detection(self, game_dir: str, info: dict[str, Any]) -> None:
        """识别结果只用于引导；真正准备时由 Core 再检查当前资源。"""
        family = info["family"]
        evidence = "、".join(str(item) for item in info.get("evidence", []))
        if family == "unknown":
            self._detected_game = None
            message = "未识别到受支持的引擎，请确认游戏子目录；本页暂不能准备。"
        else:
            self._detected_game = game_dir
            message = f"引擎：{family}；类型：{info.get('variant', '')}。依据：{evidence}"
            message += "\n已识别引擎；实际文本覆盖情况以准备报告和人工查看为准。"
        self._detection_label.setText(message)
        self._update_buttons()

    def show_detection_error(self) -> None:
        self._detected_game = None
        self._detection_label.setText("检测未完成，原因见页面提示；修改目录后可重新检测。")
        self._update_buttons()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        for widget in (self._game_input, self._work_input, self._game_browse, self._work_browse):
            widget.setEnabled(not busy)
        self._update_buttons()

    @Slot()
    def _update_buttons(self) -> None:
        game_dir = self._game_input.text().strip()
        self._detect_button.setEnabled(not self._busy and bool(game_dir))
        self._prepare_button.setEnabled(
            not self._busy and self._detected_game == game_dir and bool(self._work_input.text().strip())
        )
