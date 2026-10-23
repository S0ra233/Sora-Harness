"""设置导航先挂载空容器，首次进入才创建表单；已编辑页面保持原实例。"""
from collections.abc import Callable
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from ..common.icons import icon
from ..shell import theme
from ..sidebar.controller import CatalogController
from ..common.sidebar_splitter import SidebarSplitter
from ..common.fading_stack import FadingStack
from ..common.scrolling import SmoothScrollArea
from ..common.buttons import MotionButton
from ..common.surfaces import WhiteSurface, settings_column
from ...runtime.runs import exception_diagnostics


class SettingsPage(WhiteSurface):
    prompt_changed = Signal()
    skills_changed = Signal()
    conversation_requested = Signal(str)

    def __init__(self, model_panel: QWidget | Callable[[QWidget], QWidget], parent=None, *, catalog: CatalogController | None = None, preferences=None, presets=None, skills=None, general=None, mcp=None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self._model_source = model_panel
        self._catalog, self._presets, self._skills = catalog, presets, skills
        self._general, self._mcp = general, mcp
        self.model_panel = self.archive_page = self.prompt_page = None
        self.skill_page = self.general_page = self.mcp_page = None
        self._page_names: list[str] = []
        self._created: dict[int, QWidget] = {}
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        sidebar = QWidget(self)
        sidebar.setObjectName("settingsSidebar")
        navigation = QVBoxLayout(sidebar)
        navigation.setContentsMargins(18, 24, 18, 20)
        navigation.setSpacing(0)
        title = QLabel("设置", sidebar)
        title.setObjectName("settingsTitle")
        navigation.addWidget(title)
        navigation.addSpacing(20)
        links = QVBoxLayout()
        links.setSpacing(theme.SETTINGS_NAV_SPACING)
        navigation.addLayout(links)
        self.stack = FadingStack(self)
        group = QButtonGroup(self)
        group.setExclusive(True)
        entries = [("model", "模型", "settings", True),
                   ("general", "通用", "settings", general is not None),
                   ("prompt", "提示词", "edit", presets is not None),
                   ("skill", "技能", "file", skills is not None),
                   ("mcp", "MCP 工具", "plugin", mcp is not None),
                   ("archive", "已归档的聊天", "archive", catalog is not None)]
        for name, title, glyph, available in entries:
            if not available:
                continue
            index = self.stack.addWidget(WhiteSurface(self.stack))
            self._page_names.append(name)
            # 导航从创建开始就归侧栏所有，不会被Qt作为独立窗口显示。
            button = MotionButton(title, sidebar, variant="ghost")
            button.setObjectName("settingsNav")
            button.setIcon(icon(glyph))
            button.setCheckable(True)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setChecked(index == 0)
            group.addButton(button, index)
            if name == "general":
                links.insertWidget(0, button)
            else:
                links.addWidget(button)
        navigation.addStretch()
        group.idClicked.connect(self._select)
        self.splitter = SidebarSplitter(sidebar, self.stack, name="settings", preferences=preferences, parent=self)
        row.addWidget(self.splitter, 1)

    def _select(self, index: int) -> None:
        self._ensure_page(index)
        self.stack.setCurrentIndex(index)

    def showEvent(self, event) -> None:
        # 初次显示设置时才建立默认模型页；启动在主界面时不创建表单。
        self._ensure_page(self.stack.currentIndex())
        super().showEvent(event)

    def _ensure_page(self, index: int) -> None:
        if index in self._created:
            return
        slot = self.stack.widget(index)
        if slot.layout() is None:
            layout = QVBoxLayout(slot)
            layout.setContentsMargins(0, 0, 0, 0)
        layout = slot.layout()
        # 首次创建失败可以原位重试，不发布半份页面或改变其他页面的编辑状态。
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        container = WhiteSurface(slot)
        contents = QVBoxLayout(container)
        contents.setContentsMargins(0, 0, 0, 0)
        try:
            content = self._create_page(self._page_names[index], container)
        except Exception as exc:
            container.deleteLater()
            note = QLabel(f"设置页面创建失败（{type(exc).__name__}）。", slot)
            note.setObjectName("settingsNote")
            note.setTextFormat(Qt.TextFormat.PlainText)
            note.setToolTip(exception_diagnostics(exc))
            retry = MotionButton("重试打开", slot, variant="secondary")
            retry.clicked.connect(lambda: self._ensure_page(index))
            layout.addWidget(note)
            layout.addWidget(retry)
            layout.addStretch()
            return
        contents.addWidget(content)
        layout.addWidget(container)
        self._created[index] = container

    @staticmethod
    def _scroll(content: QWidget, parent) -> SmoothScrollArea:
        scroll = SmoothScrollArea(parent)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { background: white; border: none; }")
        scroll.setWidget(content)
        return scroll

    def _create_page(self, name: str, parent) -> QWidget:
        if name == "model":
            return self._model_page(parent)
        if name == "archive":
            from .archive_page import ArchivePage
            page = ArchivePage(self._catalog, parent)
            page.conversation_requested.connect(self.conversation_requested.emit)
            self.archive_page = page
            return page
        if name == "prompt":
            from .prompt_page import PromptPage
            page = PromptPage(self._presets, parent)
            page.changed.connect(self.prompt_changed.emit)
        elif name == "skill":
            from .skill_page import SkillPage
            page = SkillPage(self._skills, parent)
            page.changed.connect(self.skills_changed.emit)
        elif name == "mcp":
            from .mcp_page import McpPage
            page = McpPage(self._mcp, parent)
        else:
            from .general_page import GeneralPage
            page = GeneralPage(self._general, parent)
        content = self._scroll(page, parent)
        setattr(self, name + "_page", page)
        return content

    def _model_page(self, parent) -> QWidget:
        content = WhiteSurface(parent)
        content.setObjectName("settingsContent")
        column_layout = settings_column(content)
        heading = QLabel("模型")
        heading.setObjectName("settingsHeading")
        description = QLabel("管理 API 接口及模型，选择新建聊天使用的默认接口。")
        description.setObjectName("settingsDescription")
        description.setWordWrap(True)
        column_layout.addWidget(heading)
        column_layout.addWidget(description)
        column_layout.addSpacing(10)
        panel = self.model_panel
        if panel is None:
            panel = self._model_source(content) if callable(self._model_source) else self._model_source
        column_layout.addWidget(panel)
        column_layout.addStretch()
        scroll = self._scroll(content, parent)
        self.model_panel = panel
        return scroll

    def close_block(self):
        """检查实际存在的表单，不因关闭窗口而创建尚未使用的页面。"""
        for page in (self.model_panel, self.mcp_page, self.prompt_page, self.skill_page):
            if page is not None:
                block = page.close_block()
                if block is not None:
                    return block
        return None
