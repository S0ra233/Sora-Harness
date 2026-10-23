"""主应用壳层：挂载通用工作区、设置和插件自己的 UI。"""
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QCloseEvent, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QFrame, QHBoxLayout, QMainWindow, QMessageBox,
    QVBoxLayout, QWidget,
)

from ..settings.page import SettingsPage
from ..common.icons import icon
from ..common.fading_stack import FadingStack
from ..common.surfaces import RoundedSurface
from ..common.widgets import IconButton
from ..plugins.page import PluginsPage
from ..contracts import CloseBlock, UiServices
from ..pages import load_pages
from ..conversations import ConversationHub
from ..sidebar.controller import CatalogController
from ..workspace import ConversationWorkspace
from .theme import shell_stylesheet, TEXT_FAMILIES, BODY_FONT_SIZE, RAIL_WIDTH, TEXT, MUTED
from .windows_caption import hide_caption_label
from ...storage.paths import LocalPaths
from ...application.ui_preferences import UiPreferencesStore
from ...application.plugin_settings import PluginSettings


class MainWindow(QMainWindow):
    """只连接公共页面与导航，不按业务插件名称安排控件。"""

    def __init__(self, settings_path: Path) -> None:
        super().__init__()
        self.conversations = ConversationHub(settings_path.parent, self)
        from ..chat.tools.presenters import install_plugin_presenters
        install_plugin_presenters(self.conversations.application.plugin_catalog, self.conversations.application.enabled_plugin_names())
        self.setWindowTitle("")
        # 明确提供透明图标，避免Windows从Qt窗口类回退到默认小方块。
        caption_icon = QPixmap(32, 32)
        caption_icon.fill(Qt.GlobalColor.transparent)
        self.setWindowIcon(QIcon(caption_icon))
        self.setAccessibleName("模型工作平台")
        self.resize(1280, 820)
        self.setMinimumSize(860, 560)
        font = QFont()
        font.setFamilies(TEXT_FAMILIES)
        font.setPixelSize(BODY_FONT_SIZE)
        font.setStyleHint(QFont.StyleHint.SansSerif)
        self.setFont(font)
        self.setStyleSheet(shell_stylesheet())
        self._preferences = UiPreferencesStore(LocalPaths(settings_path.parent).ui_preferences)
        self._catalog = CatalogController(LocalPaths(settings_path.parent).catalog, self.conversations, self)
        self._workspace = ConversationWorkspace(settings_path, self.conversations, catalog=self._catalog, preferences=self._preferences)
        self._model_configuration_entries = []
        settings = SettingsPage(lambda parent: self._create_model_panel(settings_path, parent),
                                parent=self, catalog=self._catalog, preferences=self._preferences,
                                presets=self.conversations.application.prompt_presets,
                                skills=self.conversations.application.skills,
                                general=self.conversations.application.general_settings,
                                mcp=self.conversations.application.mcp_settings)
        settings.prompt_changed.connect(self._workspace.start.reload_prompt)
        self._settings_page = settings
        for splitter in (self._workspace.splitter, settings.splitter):
            splitter.preferences_failed.connect(lambda message: self.statusBar().showMessage(message))
        if self._preferences.error:
            self.statusBar().showMessage(self._preferences.error)
        services = UiServices(self.conversations.tasks, self.conversations.application.subagents.view_access())
        self._ui_registry, errors = load_pages(settings_path, tasks=self.conversations.tasks, services=services,
                                              catalog=self.conversations.application.plugin_catalog)
        QApplication.instance().aboutToQuit.connect(self._ui_registry.dispose)
        plugins = PluginsPage(self._ui_registry, errors, settings_path, catalog=self.conversations.application.plugin_catalog)
        self._plugins_page = plugins
        self._workspace.install_page_host(self._ui_registry, PluginSettings(settings_path.parent / "plugins.json"), services)
        # 表单已使SkillService失效，这里只通知菜单，避免重复增加来源编号。
        settings.skills_changed.connect(self._workspace.invalidate_skill_commands)
        from ...tools.catalog import create_tool_registry
        exclusive = {item.schema.name for item in create_tool_registry(self.conversations.application.plugin_catalog,
            enabled_plugins=self.conversations.application.enabled_plugin_names()).definitions() if item.execution_class == "exclusive"}
        def project_event(event):
            if event.kind != "agent" or event.data.kind != "tool_finished" or event.data.call is None:
                return
            result = event.data.result
            if (event.data.call.name in exclusive or event.data.call.name.startswith("mcp_")) and result is not None and not result.is_error:
                try:
                    owner = self.conversations.get(event.conversation_id)
                    root = owner.conversation.settings.workspace
                    if root:
                        self._workspace.page_host.invalidate_project(root)
                except ValueError:
                    pass
        self.conversations.event_received.connect(project_event)
        def project_reviewed(conversation):
            # 主Agent只读审核也可能确认子已保存的内容；结算后合并刷新，不逐条读工具重载。
            if conversation.settings.workspace:
                self._workspace.page_host.invalidate_project(conversation.settings.workspace)
        self.conversations.conversation_changed.connect(project_reviewed)
        pages = FadingStack()
        for page in (self._workspace, plugins, settings):
            pages.addWidget(page)
        rail = QFrame()
        rail.setObjectName("applicationRail")
        rail.setFixedWidth(RAIL_WIDTH)
        navigation = QVBoxLayout(rail)
        navigation.setContentsMargins(4, 16, 4, 16)
        navigation.setSpacing(10)
        group = QButtonGroup(self)
        group.setExclusive(True)
        for index, (symbol, title) in enumerate((("home", "主界面"), ("plugin", "插件"), ("settings", "设置"))):
            if index == 2:
                navigation.addStretch()
            button = IconButton(symbol, title, size=40)
            button.setIcon(icon(symbol, size=21))
            button.setIconSize(QSize(21, 21))
            button.setToolTip(title)
            button.setAccessibleName(title)
            button.setCheckable(True)
            button.toggled.connect(lambda checked, target=button, name=symbol: target.setIcon(
                icon("home-filled" if name == "home" and checked else name, TEXT if checked else MUTED, 21)))
            button.setChecked(index == 0)
            button.setFixedSize(40, 40)
            group.addButton(button, index)
            navigation.addWidget(button)
        group.idClicked.connect(pages.setCurrentIndex)
        for contribution in self._ui_registry.contributions("model_settings"):
            def configure(_checked=False, identifier=contribution.contribution_id):
                plugins.select(identifier)
                group.button(1).setChecked(True)
                pages.setCurrentIndex(1)
            self._model_configuration_entries.append((contribution.widget.windowTitle() or contribution.contribution_id, configure))
        def open_archived(identifier):
            self._workspace.open_conversation(identifier)
            group.button(0).setChecked(True)
            pages.setCurrentIndex(0)
        settings.conversation_requested.connect(open_archived)
        container = QWidget()
        container.setObjectName("applicationSurface")
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 2, 2)
        layout.setSpacing(0)
        layout.addWidget(rail)
        layout.addWidget(RoundedSurface(pages), 1)
        self.setCentralWidget(container)

    def _create_model_panel(self, settings_path: Path, parent) -> QWidget:
        """首次打开模型设置才建立表单，插件配置入口仍来自原登记结果。"""
        from ..settings.model_panel import ModelConfigPanel
        panel = ModelConfigPanel(settings_path, parent)
        panel.catalog_changed.connect(self._workspace.set_model_catalog)
        for label, callback in self._model_configuration_entries:
            panel.add_configuration_entry(label, callback)
        return panel

    def showEvent(self, event) -> None:
        super().showEvent(event)
        try:
            hide_caption_label(int(self.winId()))
        except OSError as exc:
            self.statusBar().showMessage(str(exc))

    def closeEvent(self, event: QCloseEvent) -> None:
        """询问全部已挂载界面，不根据插件内部类猜测运行或待存状态。"""
        self._settings_page.splitter.flush_preferences()
        block = self._plugins_page.close_block() or self._settings_page.close_block() or self.conversations.close_block()
        if block is not None:
            QMessageBox.information(self, block.title, block.message)
            event.ignore()
            return
        block = self._workspace.close_block()
        if block is not None:
            QMessageBox.information(self, block.title, block.message)
            event.ignore()
            return
        block = self._workspace.page_host.close_block()
        if block:
            QMessageBox.information(self, block.title, block.message)
            event.ignore()
            return
        for contribution in self._ui_registry.all_contributions():
            if contribution.close_check is None:
                continue
            try:
                block = contribution.close_check()
            except Exception as exc:
                # 无法完成检查时保留窗口，避免把检查失败当成允许关闭。
                block = CloseBlock(
                    "关闭检查失败",
                    f"{contribution.contribution_id} 的检查发生 {type(exc).__name__}，请保留窗口并检查实现。",
                )
            if block is not None:
                QMessageBox.information(self, block.title, block.message)
                event.ignore()
                return
        super().closeEvent(event)
