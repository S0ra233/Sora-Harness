"""接收项目名称与工作目录；文件夹内容仍由后续工具按授权读取。"""
from pathlib import Path

from ..common.buttons import MotionButton
from PySide6.QtWidgets import (
    QDialogButtonBox, QFileDialog, QFrame, QLabel, QLineEdit, QVBoxLayout,
)
from PySide6.QtCore import Qt

from ..common.dialogs import SurfaceDialog
from ..common.icons import icon


class ProjectDialog(SurfaceDialog):
    def __init__(self, parent=None) -> None:
        super().__init__("创建项目", parent, width=600)
        self.workspace: Path | None = None
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("项目名称")
        self._path = QLabel("在此电脑上选择工作文件夹")
        self._path.setTextFormat(Qt.TextFormat.PlainText)
        self._path.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._path.setWordWrap(True)
        choose = MotionButton("添加文件夹", variant="secondary")
        choose.setIcon(icon("folder"))
        choose.setCursor(Qt.CursorShape.PointingHandCursor)
        panel = QFrame()
        panel.setObjectName("folderPanel")
        panel.setMinimumHeight(144)
        folder_layout = QVBoxLayout(panel)
        folder_layout.setContentsMargins(24, 20, 24, 20)
        folder_layout.addStretch()
        folder_layout.addWidget(self._path)
        folder_layout.addWidget(choose, 0, Qt.AlignmentFlag.AlignCenter)
        folder_layout.addStretch()
        self._create = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._create.setText("创建项目")
        self._create.setEnabled(False)
        self.body.addWidget(self.name_input)
        self.body.addWidget(QLabel("源文件夹"))
        self.body.addWidget(panel)
        choose.clicked.connect(self._choose)
        self.name_input.textChanged.connect(self._update_enabled)
        self.buttons.accepted.connect(self.accept)

    def _choose(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择项目工作目录")
        if path:
            self.workspace = Path(path)
            self._path.setText(path)
            if not self.name_input.text().strip():
                self.name_input.setText(self.workspace.name)
            self._update_enabled()

    def _update_enabled(self) -> None:
        self._create.setEnabled(self.workspace is not None and bool(self.name_input.text().strip()))
