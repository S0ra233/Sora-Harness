"""插件管理页只管理开关与设置；辅助页面由统一宿主呼出。"""
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QButtonGroup, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from ..common.buttons import MotionButton
from ..common.fading_stack import FadingStack
from ..common.icons import icon
from ..common.popup import CardPopup
from ..common.widgets import IconButton
from ..common.scrolling import SmoothScrollArea
from ..common.sidebar_splitter import SidebarSplitter
from ..common.surfaces import CardSurface, WhiteSurface, settings_column
from ..common.switch import SwitchToggle
from ...plugin_catalog import load_plugin_catalog
from ...application.plugin_settings import PluginSettings
from ..shell import theme


class PluginsPage(QWidget):
    def __init__(self, registry, errors, settings_path, parent=None, *, catalog=None) -> None:
        super().__init__(parent)
        self.setObjectName("pluginsPage")
        from ..common.background_read import BackgroundRead
        self._install_job = BackgroundRead(self)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        sidebar = QWidget()
        sidebar.setObjectName("settingsSidebar")
        navigation = QVBoxLayout(sidebar)
        navigation.setContentsMargins(18, 24, 18, 20)
        navigation.setSpacing(12)
        title = QLabel("插件")
        title.setObjectName("settingsTitle")
        search = QLineEdit()
        search.setObjectName("pluginSearch")
        search.setPlaceholderText("搜索已安装插件")
        heading_row = QHBoxLayout()
        heading_row.addWidget(title, 1)
        more = IconButton("more", "插件管理操作", sidebar)
        heading_row.addWidget(more)
        navigation.addLayout(heading_row)
        navigation.addWidget(search)
        caption = QLabel("已安装")
        caption.setObjectName("settingsNote")
        popup = CardPopup(more, "pluginManagementMenu")
        actions = QVBoxLayout(popup)
        actions.setContentsMargins(12, 12, 12, 12)
        actions.setSpacing(2)
        def toggle_more():
            if popup.is_open:
                popup.dismiss(restore_focus=True)
                return
            popup.adjustSize()
            anchor = more.mapToGlobal(QPoint(more.width(), more.height() + 2))
            available = more.screen().availableGeometry()
            popup.move(max(available.left() + 4, anchor.x() - popup.width()),
                       min(anchor.y(), available.bottom() - popup.height() - 4))
            popup.present()
        more.clicked.connect(toggle_more)
        popup.bind_toggle(toggle_more)
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        extension_directory = settings_path.parent / "extensions"
        def open_extensions():
            popup.dismiss()
            try:
                extension_directory.mkdir(parents=True, exist_ok=True)
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(extension_directory)))
            except OSError as exc:
                error.setText(f"扩展目录无法打开：{exc}")
                error.show()
        directory_button = MotionButton("打开扩展目录", popup, variant="ghost", size="medium")
        directory_button.clicked.connect(open_extensions)
        actions.addWidget(directory_button)
        install_file = MotionButton("安装插件包…", sidebar, variant="secondary", size="medium")
        install_folder = MotionButton("从目录安装…", popup, variant="ghost", size="medium")
        navigation.addWidget(install_file)
        actions.addWidget(install_folder)
        navigation.addWidget(caption)
        def install(folder=False):
            popup.dismiss()
            if self._install_job.worker:
                return
            source = QFileDialog.getExistingDirectory(self, "选择单个插件目录") if folder else QFileDialog.getOpenFileName(self, "选择插件包", "", "插件包 (*.zip)")[0]
            if not source:
                return
            from ...application.extension_packages import install_package
            error.setText("正在校验并安装；完成后重启应用加载。")
            error.show()
            install_file.setEnabled(False)
            install_folder.setEnabled(False)
            self._install_job.start(lambda: install_package(settings_path.parent, source))
        install_file.clicked.connect(lambda: install())
        install_folder.clicked.connect(lambda: install(True))
        self._install_job.settled.connect(lambda: (install_file.setEnabled(True), install_folder.setEnabled(True)))
        entries = QWidget()
        entries.setObjectName("pluginNavEntries")
        entries.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        buttons = QVBoxLayout(entries)
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(theme.SETTINGS_NAV_SPACING)
        menu = SmoothScrollArea()
        menu.setObjectName("pluginNavScroll")
        menu.viewport().setObjectName("pluginNavViewport")
        menu.viewport().setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        menu.setFrameShape(SmoothScrollArea.Shape.NoFrame)
        menu.setWidgetResizable(True)
        menu.setWidget(entries)
        navigation.addWidget(menu, 1)
        body = WhiteSurface()
        contents = QVBoxLayout(body)
        contents.setContentsMargins(0, 0, 0, 0)
        contents.setSpacing(0)
        error = QLabel("插件界面加载失败：\n" + "\n".join(errors) if errors else "", body)
        error.setObjectName("chatStatus")
        error.setTextFormat(Qt.TextFormat.PlainText)
        error.setWordWrap(True)
        error.setContentsMargins(theme.SETTINGS_PAGE_MARGINS[0], 8, theme.SETTINGS_PAGE_MARGINS[2], 0)
        error.setVisible(bool(errors))
        self._install_job.completed.connect(lambda result: error.setText(f"{result['name']} · {result['version']}：{result['note']}"))
        self._install_job.failed.connect(error.setText)
        contents.addWidget(error)
        self.stack = FadingStack()
        self._settings = PluginSettings(settings_path.parent / "plugins.json")
        self._page_ids = {}
        self._buttons = []
        group = QButtonGroup(self)
        settings = {entry.contribution_id: entry for entry in registry.contributions("model_settings")}
        legacy = {entry.contribution_id: entry for entry in registry.contributions("workspace")}
        for position, descriptor in enumerate((catalog or load_plugin_catalog(settings_path.parent)).descriptors):
            entry = legacy.get(descriptor.plugin_id)
            name = descriptor.label or (entry.widget.windowTitle() if entry is not None else descriptor.plugin_id)
            panel = WhiteSurface()
            column = settings_column(panel)
            heading = QLabel(name)
            heading.setObjectName("settingsHeading")
            column.addWidget(heading)
            metadata = QLabel(f"版本：{descriptor.version}　接口：v{descriptor.api_version}\n来源：{descriptor.host.code_directory if descriptor.host else '应用内置'}")
            metadata.setObjectName("settingsNote")
            metadata.setTextFormat(Qt.TextFormat.PlainText)
            metadata.setWordWrap(True)
            metadata.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            column.addWidget(metadata)
            card_body = QWidget()
            card = QHBoxLayout(card_body)
            labels = QVBoxLayout()
            labels.addWidget(QLabel("启用插件"))
            description = QLabel(descriptor.description or "管理插件是否可用；正常请求仍在主聊天中进行。")
            description.setObjectName("settingsNote")
            description.setWordWrap(True)
            labels.addWidget(description)
            card.addLayout(labels, 1)
            switch = SwitchToggle("启用" + name)
            try:
                switch.setChecked(self._settings.enabled(descriptor.plugin_id, default=descriptor.default_enabled))
            except (OSError, ValueError) as exc:
                switch.setEnabled(False)
                error.setText(f"插件开关读取失败：{exc}；原文件保留。")
                error.show()
            switch.toggled.connect(lambda enabled, item=descriptor, control=switch: self._toggle(item, control, enabled))
            card.addWidget(switch)
            column.addWidget(CardSurface(card_body))
            if descriptor.host is not None:
                note = QLabel("本地Python插件；开关、清单或代码修改后重启应用加载。插件依赖由安装者提供。")
                note.setObjectName("settingsNote")
                note.setWordWrap(True)
                column.addWidget(note)
            configuration = settings.get(descriptor.plugin_id)
            if configuration is not None:
                column.addWidget(configuration.widget)
            else:
                pages = [item for item in registry.pages() if item.plugin_id == descriptor.plugin_id]
                hint = "；".join("/" + item.aliases[0] + " 打开" + item.title for item in pages)
                note = QLabel("此插件暂无额外配置。" + ("聊天中输入 " + hint + "。" if hint else ""))
                note.setObjectName("settingsNote")
                note.setWordWrap(True)
                column.addWidget(note)
            column.addStretch()
            scroll = SmoothScrollArea()
            scroll.setFrameShape(SmoothScrollArea.Shape.NoFrame)
            scroll.setWidgetResizable(True)
            scroll.setWidget(panel)
            self._page_ids[descriptor.plugin_id] = self.stack.addWidget(scroll)
            button = MotionButton(name, variant="ghost")
            button.setProperty("pluginId", descriptor.plugin_id)
            button.setObjectName("pluginNav")
            button.setIcon(icon("plugin"))
            button.setCheckable(True)
            button.setChecked(position == 0)
            button.setToolTip(name)
            group.addButton(button)
            button.clicked.connect(lambda checked=False, identifier=descriptor.plugin_id: self.select(identifier))
            self._buttons.append((name, button))
            buttons.addWidget(button)
        buttons.addStretch()
        if not self._page_ids:
            empty = QLabel("暂无已登记的插件界面")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setObjectName("settingsNote")
            self.stack.addWidget(empty)
        contents.addWidget(self.stack, 1)
        search.textChanged.connect(self._filter)
        self.splitter = SidebarSplitter(sidebar, body, name="plugins")
        row.addWidget(self.splitter, 1)

    def close_block(self):
        return self._install_job.close_block()

    def select(self, identifier: str) -> None:
        self.stack.setCurrentIndex(self._page_ids[identifier])
        for _name, button in self._buttons:
            button.setChecked(self._page_ids[identifier] == self.stack.currentIndex() and button.property("pluginId") == identifier)

    def _filter(self, text: str) -> None:
        query = text.strip().casefold()
        for name, button in self._buttons:
            button.setVisible(query in name.casefold())

    def _toggle(self, descriptor, control, enabled):
        try:
            self._settings.set_enabled(descriptor.plugin_id, enabled)
        except (OSError, ValueError) as exc:
            control.blockSignals(True)
            control.setChecked(not enabled)
            control.blockSignals(False)
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, "插件设置未保存", str(exc))
