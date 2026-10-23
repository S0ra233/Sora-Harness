"""主工作区连接目录、侧栏和固定会话控制器；不包含业务插件布局。"""
from pathlib import Path

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, Property, QPropertyAnimation, Qt, QTimer, Slot, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from ..common.buttons import MotionButton
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QMenu, QSplitter, QVBoxLayout, QWidget,
)

from ...runtime.conversations import ConversationSettings
from ...storage.paths import LocalPaths
from ..contracts import CloseBlock, UiPageContext
from ..chat import ChatPanel
from ..chat.start_page import NewChatPage
from ..conversations import ConversationController, ConversationHub, ConversationState
from ...tools.catalog import default_tool_names
from ..sidebar import ConversationSidebar
from ..sidebar.controller import CatalogController, CatalogState
from ..sidebar.project_dialog import ProjectDialog
from ..common.dialogs import ArchiveConfirmation, NameDialog
from ..common.sidebar_splitter import SidebarSplitter
from ..common.fading_stack import FadingStack
from ..common.widgets import ElidedLabel, IconButton, IconGlyph
from ..common.icons import icon
from ..shell import theme


class ConversationWorkspace(QWidget):
    """当前显示绑定属于界面；执行对象与隐藏会话仍由 Hub 保管。"""

    def __init__(self, settings_path: Path, hub: ConversationHub, parent=None, *, catalog: CatalogController | None = None,
                 preferences=None) -> None:
        super().__init__(parent)
        self.setObjectName("conversationWorkspace")
        self._settings_path, self._hub = settings_path, hub
        self._preferences = preferences
        self._catalog = catalog if catalog is not None else CatalogController(LocalPaths(settings_path.parent).catalog, hub, self)
        self._controller: ConversationController | None = None
        self._creating: ConversationController | None = None
        self._first_message = None
        self._start_visible = True
        self._record_start_next = False
        self._route_message = ""
        self._build()
        self._sidebar_open = True
        self._sidebar_toggle = IconButton("sidebar", "切换会话侧栏", parent=self)
        self._sidebar_toggle.clicked.connect(self._toggle_sidebar)
        self._sidebar_toggle.hide()
        self._sidebar_animation = QPropertyAnimation(self, b"sidebarWidth", self)
        self._sidebar_animation.setDuration(theme.SIDEBAR_WIDTH_DURATION_MS)
        self._sidebar_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._sidebar_animation.finished.connect(self._finish_sidebar)
        self._sidebar_toggle.pressed.connect(self._sidebar_animation.stop)
        self._sidebar_toggle.released.connect(lambda: QTimer.singleShot(0, self._resume_sidebar))
        self._catalog.catalog_changed.connect(self.sidebar.set_snapshot)
        self._catalog.catalog_changed.connect(lambda snapshot: self.start.set_projects(snapshot.projects))
        self._catalog.state_changed.connect(self._on_catalog_state)
        self._catalog.archiving_changed.connect(lambda _identifier, _pending: self._show_current())
        hub.state_changed.connect(self._on_state)
        hub.branch_created.connect(self._open_branch)
        self._catalog.refresh()

    def _build(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.sidebar = ConversationSidebar()
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        header = QFrame()
        self._header = header
        header.setObjectName("workspaceHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(24, 14, 24, 14)
        header_layout.setSpacing(10)
        self._expand = IconButton("sidebar", "展开会话侧栏")
        self._expand.hide()
        self._heading = ElidedLabel("主界面")
        self._heading.setObjectName("workspaceHeading")
        self._search = IconButton("search", "搜索当前对话 · Ctrl+F")
        self._more = IconButton("more", "会话选项")
        self._fullscreen = IconButton("expand", "全屏查看")
        self._fullscreen.setCheckable(True)
        self._previous_window_state = Qt.WindowState.WindowNoState
        header_layout.addWidget(self._expand)
        self._scope_icon = QLabel()
        self._scope_icon.setFixedSize(18, 18)
        self._scope_icon.setPixmap(icon("chat", theme.TEXT, 18).pixmap(18, 18))
        header_layout.addWidget(self._scope_icon)
        header_layout.addWidget(self._heading, 1)
        header_layout.addWidget(self._more)
        header_layout.addWidget(self._search)
        separator = QFrame()
        separator.setFixedSize(1, 18)
        separator.setStyleSheet(f"background: {theme.BORDER}; border: none;")
        header_layout.addWidget(separator)
        header_layout.addWidget(self._fullscreen)
        body_layout.addWidget(header)
        self._status = QLabel()
        self._status.setObjectName("workspaceNotice")
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setWordWrap(True)
        self._status.setContentsMargins(27, 4, 27, 8)
        self._status.hide()
        self.chat = ChatPanel(self._hub.application)
        self.chat.branch_requested.connect(self._fork)
        self.start = NewChatPage(self._settings_path, self._hub.application)
        self.chat.set_model_catalog(self.start.model_catalog)
        self.start.submitted.connect(self._submit_start)
        self.start.chat_requested.connect(lambda: self.new_conversation(None))
        self.start.work_requested.connect(self._choose_work)
        self.start.project_requested.connect(self._choose_project)
        self._pages = FadingStack()
        self._pages.addWidget(self.chat)
        self._pages.addWidget(self.start)
        self._pages.setCurrentWidget(self.start)
        body_layout.addWidget(self._pages, 1)
        body_layout.addWidget(self._status)
        notice_row = QWidget()
        self._notice_row = notice_row
        notice_layout = QHBoxLayout(notice_row)
        notice_layout.setContentsMargins(20, 8, 20, 16)
        self._notice = QLabel()
        self._notice.setObjectName("workspaceNotice")
        self._notice.setWordWrap(True)
        self._notice.setTextFormat(Qt.TextFormat.PlainText)
        self._retry = MotionButton("重试目录", variant="secondary")
        self._retry.setObjectName("retryButton")
        self._retry.hide()
        notice_layout.addWidget(self._notice, 1)
        notice_layout.addWidget(self._retry)
        body_layout.addWidget(notice_row)
        notice_row.hide()
        self._chat_body = body
        self._aside_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._aside_splitter.setHandleWidth(1)
        self._aside_splitter.setStyleSheet("QSplitter::handle { background: #e7e9ec; border: none; }")
        self._aside_splitter.addWidget(body)
        self.splitter = SidebarSplitter(self.sidebar, self._aside_splitter, name="conversation", preferences=self._preferences)
        layout.addWidget(self.splitter, 1)
        self.sidebar.conversation_requested.connect(self.open_conversation)
        self.sidebar.new_requested.connect(self.new_conversation)
        self.sidebar.project_requested.connect(self._add_project)
        self.sidebar.rename_requested.connect(self.rename_conversation)
        self.sidebar.pin_requested.connect(self._pin)
        self.sidebar.archive_requested.connect(self._archive)
        self.sidebar.collapse_requested.connect(self._toggle_sidebar)
        self.sidebar.refresh_requested.connect(self._catalog.refresh)
        self._expand.clicked.connect(self._toggle_sidebar)
        self._retry.clicked.connect(self._catalog.refresh)
        self._search.clicked.connect(self.chat.search.open_search)
        self._more.clicked.connect(self._current_menu)
        self._fullscreen.toggled.connect(self._toggle_fullscreen)
        title_search = QShortcut(QKeySequence("Ctrl+Shift+F"), self)
        title_search.activated.connect(self._open_title_search)

    def page_context(self):
        conversation = self._controller.conversation if self._controller is not None and not self._start_visible else None
        if conversation is not None:
            return UiPageContext(conversation_id=conversation.conversation_id, workspace=conversation.settings.workspace,
                                 mode=conversation.settings.prompt_mode)
        return UiPageContext(project_id=self.start.project_id,
                             workspace=self.start._scope_name.toolTip() or None,
                             mode="work" if self.start.project_id else "dialogue")

    def install_page_host(self, registry, settings, services):
        from ..common.panel_host import PageHost
        from ..chat.commands import LocalCommand, PageCommands
        from ..chat.skill_commands import SkillCommandSource
        self.page_host = PageHost(registry, settings, services, self, self._aside_splitter, self.page_context)
        def open_reference(page_id, resource_id):
            try:
                self.page_host.open_page(page_id, resource_id=resource_id)
            except (OSError, ValueError) as exc:
                error(str(exc))
        self.chat.page_requested.connect(open_reference)
        decorators = tuple(item.decorate_rows for item in registry.all_contributions() if item.decorate_rows is not None)
        def decorate(rows, context):
            for callback in decorators:
                rows = callback(rows, context)
            return rows
        self.chat.row_decorator = decorate
        self._hub.subagent_changed.connect(lambda _: self.chat.refresh_decorations())
        def error(message):
            self._route_error(message)
        for composer in (self.chat.composer, self.start.composer):
            reason = self.chat.compact_unavailable if composer is self.chat.composer else lambda: "请先开始对话，积累已结束历史后再压缩。"
            compact = LocalCommand("compact", "压缩", ("compact", "压缩"), "压缩此聊天的上下文", "compact",
                                   self.chat.start_compaction, reason, whole_input=True, show_unavailable=True)
            source = SkillCommandSource(self._hub.application.skills, self)
            composer.page_commands = PageCommands(composer, self.page_host, error, commands=(compact,),
                                                   command_source=source)
        buttons = []
        header = self._chat_body.layout().itemAt(0).widget().layout()
        for definition in registry.pages():
            if definition.presentation != "aside":
                continue
            button = IconButton("plugin", definition.title)
            button.setCheckable(True)
            def open_page(_checked=False, page_id=definition.page_id):
                try:
                    self.page_host.toggle_page(page_id)
                except (OSError, ValueError) as exc:
                    error(str(exc))
                    sync_buttons(self.page_host.active_page)
            button.clicked.connect(open_page)
            header.insertWidget(header.count() - 1, button)
            buttons.append((definition, button))
        def sync_buttons(active):
            for definition, button in buttons:
                button.setChecked(definition.page_id == active)
        self.page_host.aside_changed.connect(sync_buttons)
        def rebind(_=None):
            self.page_host.context_changed()
            context = self.page_context()
            for definition, button in buttons:
                try:
                    button.setVisible(self.page_host.available(definition, context) is None)
                except (OSError, ValueError):
                    button.hide()
        self._hub.state_changed.connect(rebind)
        self._hub.conversation_changed.connect(rebind)
        self._hub.subagent_changed.connect(rebind)
        self.start.chat_requested.connect(rebind)
        self.start.project_requested.connect(rebind)
        rebind()

    def _toggle_sidebar(self) -> None:
        self._show_sidebar(not self._sidebar_open)

    def _show_sidebar(self, visible: bool) -> None:
        if visible == self._sidebar_open and self._sidebar_animation.state() == QAbstractAnimation.State.Running:
            return
        if not self._sidebar_toggle.isVisible():
            source = self.sidebar.collapse_button if self._sidebar_open else self._expand
            self._sidebar_toggle.move(self.mapFromGlobal(source.mapToGlobal(source.rect().topLeft())))
        self._sidebar_open = visible
        self._sidebar_animation.stop()
        start = self.splitter.extent
        self.splitter.begin_transition()
        self.splitter.set_extent(start)
        self._expand.setVisible(not visible)
        self._sidebar_toggle.setToolTip("收起会话侧栏" if visible else "展开会话侧栏")
        self._sidebar_toggle.show()
        self._sidebar_toggle.raise_()
        end = self.splitter.preferred_width if visible else 0
        full = max(1, self.splitter.preferred_width, start)
        self._sidebar_animation.setDuration(max(1, round(theme.SIDEBAR_WIDTH_DURATION_MS * abs(end - start) / full)))
        self._sidebar_animation.setStartValue(start)
        self._sidebar_animation.setEndValue(end)
        self._sidebar_animation.start()

    @Property(int)
    def sidebarWidth(self) -> int:
        return self.splitter.extent

    @sidebarWidth.setter
    def sidebarWidth(self, value: int) -> None:
        self.splitter.set_extent(value)

    def _finish_sidebar(self) -> None:
        self.splitter.end_transition(self._sidebar_open)
        self._sidebar_toggle.hide()

    def _resume_sidebar(self) -> None:
        if self._sidebar_toggle.isVisible() and self._sidebar_animation.state() != QAbstractAnimation.State.Running:
            self._show_sidebar(self._sidebar_open)

    def hideEvent(self, event) -> None:
        self._sidebar_animation.stop()
        self._finish_sidebar()
        super().hideEvent(event)

    def _open_title_search(self) -> None:
        self._show_sidebar(True)
        self.sidebar.open_search()

    def _toggle_fullscreen(self, checked: bool) -> None:
        window = self.window()
        if checked:
            self._previous_window_state = window.windowState()
            window.showFullScreen()
        else:
            window.setWindowState(self._previous_window_state)
        self._fullscreen.setToolTip("退出全屏" if checked else "全屏查看")

    def _current_menu(self) -> None:
        menu = QMenu(self)
        controller = self._controller
        identifier = controller.state.conversation_id if controller is not None and not self._start_visible else None
        try:
            entry = self._catalog.service.conversation(identifier) if identifier is not None and self._catalog.service is not None else None
        except KeyError:
            entry = None
        if entry is not None and not entry.archived and not entry.deleted and self._catalog.state.ready and not self._catalog.state.busy and not self._catalog.is_archiving(identifier):
            menu.addAction("编辑名称", self._rename_current)
            menu.addAction("取消置顶" if entry.pinned else "置顶聊天", lambda: self._pin(identifier))
            menu.addAction("归档聊天", lambda: self._archive(identifier))
            menu.addSeparator()
        menu.addAction("搜索对话", self.chat.search.open_search).setEnabled(not self._start_visible)
        menu.addAction("显示侧栏" if not self._sidebar_open else "收起侧栏",
                       lambda: self._show_sidebar(not self._sidebar_open))
        menu.addSeparator()
        record = menu.addAction("记录下一轮请求")
        record.setCheckable(True)
        record.setChecked(self._record_start_next if self._start_visible else bool(controller and controller.state.record_next_turn))
        record.setEnabled(self._creating is None if self._start_visible else controller is not None)
        record.toggled.connect(self._set_record_next_turn)
        folder = menu.addAction("打开请求记录目录", self._open_request_records)
        folder.setEnabled(identifier is not None and controller.state.record_run_id is not None)
        if controller is not None and not self._start_visible:
            menu.addSeparator()
            self._preset_menu(menu, controller)
            backup = (controller.conversation.navigation or {}).get("displaced_draft") if controller.conversation else None
            if backup and (backup["text"] or backup["images"]):
                menu.addAction("恢复回退前的草稿", controller.restore_displaced_draft).setEnabled(not controller.busy)
            if controller.can_retry_revision:
                menu.addAction("重试保存对话修订", controller.retry_revision).setEnabled(not controller.busy)
            if controller.can_retry_fork:
                menu.addAction("重试保存分支", controller.retry_fork)
            if controller.can_retry_regeneration:
                menu.addAction("重试重新执行", controller.retry_regeneration).setEnabled(not controller.busy)
        menu.exec(self._more.mapToGlobal(self._more.rect().bottomLeft()))
        menu.deleteLater()

    def _fork(self, anchor) -> None:
        try:
            entry = self._catalog.service.conversation(self._controller.state.conversation_id)
            self._controller.fork(anchor, title=entry.title + "（分支）")
        except (OSError, ValueError, KeyError) as exc:
            self._route_error(f"分支尚未创建：{exc}")

    def _open_branch(self, update) -> None:
        from ...runtime.conversations.models import ConversationSummary
        if update.conversation is None:
            return
        identifier = update.conversation.conversation_id
        self._catalog.change(lambda: self._catalog.service.observe((ConversationSummary.from_conversation(update.conversation),)))
        if self._catalog.state.error:
            self._route_error(f"分支 {identifier} 已保存，目录尚未保存；请重试目录。")
            return
        self.open_conversation(identifier)

    def _preset_menu(self, menu, controller) -> None:
        conversation = controller.conversation
        submenu = menu.addMenu("预设")
        submenu.menuAction().setToolTip("当前预设：" + conversation.current_preset_name if conversation else "正在读取会话预设")
        submenu.setToolTipsVisible(True)
        submenu.setEnabled(conversation is not None and not controller.busy)
        if conversation is None:
            return
        current = submenu.addAction("当前：" + conversation.current_preset_name)
        current.setEnabled(False)
        submenu.addSeparator()
        try:
            presets, _ = self._hub.application.prompt_presets.load()
            for preset in presets:
                action = submenu.addAction(preset.name)
                action.setCheckable(True)
                action.setChecked(preset.preset_id == conversation.current_preset_id)
                action.setToolTip("采用此预设已保存的规则与画像；保留本会话预对话和历史。")
                action.triggered.connect(lambda _checked=False, value=preset: self._apply_prompt_preset(value))
        except (OSError, ValueError) as exc:
            action = submenu.addAction("预设读取失败")
            action.setEnabled(False)
            action.setToolTip(str(exc))

    def _apply_prompt_preset(self, preset) -> None:
        controller = self._controller
        if controller is None or controller.busy:
            return
        try:
            controller.apply_prompt(preset.config, preset_id=preset.preset_id, preset_name=preset.name)
        except (OSError, ValueError) as exc:
            self._route_error(f"提示词预设未应用：{exc}")

    def _set_record_next_turn(self, enabled: bool) -> None:
        if self._start_visible:
            self._record_start_next = enabled
        elif self._controller is not None:
            self._controller.set_record_next_turn(enabled)

    def _open_request_records(self) -> None:
        controller = self._controller
        if self._start_visible or controller is None or controller.state.record_run_id is None:
            return
        try:
            directory = self._hub.application.request_record_directory(
                controller.state.conversation_id, controller.state.record_run_id)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory))):
                raise OSError("系统未能打开请求记录目录。")
        except (OSError, ValueError) as exc:
            self._route_error(f"请求记录目录打开失败：{exc}")

    @Slot(object)
    def _on_catalog_state(self, state: CatalogState) -> None:
        self.sidebar.setEnabled(state.ready and not state.busy)
        self._notice.setText(state.error or "")
        self._notice_row.setVisible(bool(state.error))
        self._retry.setVisible(state.error is not None)
        self._retry.setText("重试保存目录" if state.pending_save else "重试目录")
        self._retry.setEnabled(not state.busy)
        self._show_current()

    @Slot()
    def _add_project(self, *, select_new: bool = False) -> None:
        if not self._catalog.state.ready or self._catalog.state.busy or self._creating is not None:
            return
        dialog = ProjectDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            name, workspace = dialog.name_input.text(), dialog.workspace
            created = []
            self._catalog.change(lambda: created.append(self._catalog.service.add_project(name, workspace)))
            if select_new and created:
                self.new_conversation(created[0].project_id)
        dialog.deleteLater()

    def _choose_work(self) -> None:
        if not self._catalog.state.ready or self._catalog.state.busy or self._creating is not None:
            return
        if not self._catalog.snapshot().projects:
            self._add_project(select_new=True)
        else:
            self.start.choose_work_project()

    def _choose_project(self, identifier) -> None:
        if identifier is None:
            self._add_project(select_new=True)
        elif identifier:
            self.new_conversation(identifier)

    @Slot(object)
    def new_conversation(self, project_id: str | None = None) -> None:
        self._route_error("")
        if not self._catalog.state.ready or self._catalog.state.busy or self._creating is not None:
            return
        try:
            project = self._catalog.service.project(project_id) if project_id is not None else None
            if self._start_visible and self.start.project_id == project_id:
                return
            self._start_visible = True
            self.start.flush_drafts()
            self.start.set_scope(project_id, project.name if project else None, project.workspace if project else None)
            self._pages.setCurrentWidget(self.start)
            self._show_current()
            self.start.composer.editor.setFocus()
        except (OSError, ValueError) as exc:
            self._route_error(str(exc))

    def set_model_catalog(self, catalog) -> None:
        """成功保存的候选一次通知两个入口；控制器和活动运行不被改写。"""
        self.start.reload_models(catalog)
        self.chat.set_model_catalog(catalog)

    def _submit_start(self, selection, text: str, images) -> None:
        if self._creating is not None or not self._catalog.state.ready or self._catalog.state.busy:
            return
        try:
            project = self._catalog.service.project(self.start.project_id) if self.start.project_id is not None else None
            settings = ConversationSettings(
                config=selection.config, workspace=project.workspace if project else None,
                tool_names=list(self._hub.application.default_tool_names()) if project else [], prompt_config=self.start.prompt_config(),
                prompt_mode="work" if project else "dialogue", reasoning_effort=selection.effort,
                model_id=selection.model_id, connection_id=selection.connection_id,
                model_label=selection.label, connection_label=selection.connection_label,
                prompt_preset_id=self.start.prompt_preset_id, prompt_preset_name=self.start.prompt_preset_name,
            )
            controller = self._hub.new_conversation(settings)
            self._creating = controller
            self._first_message = (text, images, self._record_start_next)
            self.start.set_creating(True)
            controller.task_finished.connect(self._finish_create)
            self._bind(controller, show_chat=False)
        except (OSError, ValueError) as exc:
            self.start.set_creating(False, str(exc))

    @Slot(str)
    def _finish_create(self, _operation_id: str) -> None:
        controller = self._creating
        controller.task_finished.disconnect(self._finish_create)
        self._creating = None
        text, images, record_requests = self._first_message
        self._first_message = None
        if controller.conversation is None:
            self.start.set_creating(False, controller.state.error or "会话未创建，输入保留。")
            self._show_current()
            return
        try:
            controller.set_record_next_turn(record_requests)
            if record_requests:
                self._record_start_next = False
            controller.set_draft(text, list(images))
            if not controller.flush_drafts():
                raise ValueError(controller.state.draft_error or "草稿未保存。")
            self.start.mark_transferred()
            self.start.set_creating(False)
            if self._controller is controller:
                self._start_visible = False
                self._pages.setCurrentWidget(self.chat)
            identifier = controller.state.conversation_id
            try:
                entry = self._catalog.service.conversation(identifier)
            except KeyError:
                entry = None
            if self._catalog.is_archiving(identifier) or entry is not None and (entry.archived or entry.deleted):
                raise ValueError("此聊天正在归档或已经隐藏，首条消息尚未发送；草稿已保留。")
            controller.send_message(text, list(images))
        except (OSError, ValueError) as exc:
            # 已创建的会话继续持有输入，重试发送不能再创建另一个会话。
            self.start.set_creating(False)
            if self._controller is controller:
                self._start_visible = False
                self._pages.setCurrentWidget(self.chat)
            self._route_error(f"聊天 {controller.state.conversation_id} 首次发送未开始：{exc}")
        self._show_current()

    @Slot(str)
    def open_conversation(self, conversation_id: str) -> None:
        self._route_error("")
        try:
            entry = self._catalog.service.conversation(conversation_id)
            if entry.deleted:
                raise ValueError("该聊天已从界面移除。")
        except (AttributeError, KeyError, ValueError) as exc:
            self._route_error(f"无法打开聊天：{exc}")
            return
        if self._controller is not None and self._controller.state.conversation_id == conversation_id:
            self._start_visible = False
            self._pages.setCurrentWidget(self.chat)
            if self._controller.conversation is None and not self._controller.busy:
                try:
                    self._controller.open_conversation(conversation_id)
                except (OSError, ValueError) as exc:
                    self._route_error(str(exc))
            self._show_current()
            return
        try:
            controller = self._hub.open_conversation(conversation_id)
            self._bind(controller)
        except (OSError, ValueError) as exc:
            self._route_error(f"会话 {conversation_id} 无法打开：{exc}")

    def _route_error(self, text: str) -> None:
        self._route_message = text
        self._render_status()

    def _render_status(self) -> None:
        warning = self._controller.state.record_warning if self._controller is not None and not self._start_visible else None
        text = "\n".join(value for value in (self._route_message, warning) if value)
        if self._status.text() != text:
            self._status.setText(text)
        self._status.setVisible(bool(text))

    def _bind(self, controller: ConversationController, *, show_chat: bool = True) -> None:
        previous = self._controller
        self.chat.unbind()
        if previous is not None:
            previous.state_changed.disconnect(self._show_current)
            previous.conversation_changed.disconnect(self._show_current)
            self._hub.release_view(previous)
        self._controller = controller
        if show_chat:
            self._start_visible = False
            self._pages.setCurrentWidget(self.chat)
        self.chat.set_read_only(None)
        self.chat.bind(controller)
        controller.state_changed.connect(self._show_current)
        controller.conversation_changed.connect(self._show_current)
        self._show_current()

    @Slot(object)
    def _on_state(self, state: ConversationState) -> None:
        if state.conversation_id is not None:
            try:
                controller = self._hub.get(state.conversation_id)
            except ValueError:
                return
            self.sidebar.set_running(state.conversation_id, controller.busy)

    def _show_current(self, _value=None) -> None:
        for composer in (self.chat.composer, self.start.composer):
            if composer.page_commands is not None:
                composer.page_commands.context_changed()
        self._render_status()
        if self._start_visible:
            self.sidebar.set_active(None)
            self.sidebar.set_new_enabled(self._creating is None and self.start.project_id is not None)
            self._heading.setText("新聊天")
            self._heading.setToolTip("新聊天")
            self._scope_icon.setPixmap(icon("chat", theme.TEXT, 18).pixmap(18, 18))
            self._search.hide()
            return
        self._search.show()
        controller = self._controller
        conversation = controller.conversation if controller else None
        identifier = controller.state.conversation_id if controller else None
        try:
            entry = self._catalog.service.conversation(identifier) if self._catalog.service is not None and identifier is not None else None
        except KeyError:
            # Core 创建事实先于目录登记，目录失败不能伪造会话编号或重建日志。
            entry = None
        if entry is not None and entry.deleted:
            self._clear_current()
            return
        self.sidebar.set_active(identifier if entry is None or not entry.archived else None)
        reason = ("正在停止并归档，请等待任务退出。" if identifier is not None and self._catalog.is_archiving(identifier)
                  else "此聊天已归档，可以在设置中恢复后继续发送。" if entry is not None and entry.archived else None)
        self.chat.set_read_only(reason)
        empty_plain = conversation is not None and reason is None and conversation.settings.workspace is None and not conversation.turns and not controller.has_live_run
        self.sidebar.set_new_enabled(self._creating is None and not empty_plain)
        if controller is None:
            return
        if conversation is None:
            self._heading.setText("会话")
            self._heading.setToolTip("正在准备会话…" if controller.busy else "会话尚未打开")
            self.chat.set_scope(None)
        else:
            title = entry.title if entry else "新聊天"
            self._heading.setText(title)
            workspace = conversation.settings.workspace
            project = self._catalog.service.project(entry.project_id) if entry is not None and entry.project_id is not None else None
            project_name = project.name if project is not None else Path(workspace).name if workspace else None
            self._heading.setToolTip(f"{title}\n{workspace or '普通聊天'}\n最近运行模型：{conversation.last_options.model_target.config.model} · 已记录 {len(conversation.turns)} 轮对话")
            self._scope_icon.setPixmap(icon("folder" if workspace else "chat", theme.TEXT, 18).pixmap(18, 18))
            self.chat.set_scope(workspace, project_name)

    @Slot(str)
    def rename_conversation(self, conversation_id: str) -> None:
        entry = self._catalog.service.conversation(conversation_id)
        dialog = NameDialog(entry.title, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._catalog.change(lambda: self._catalog.service.rename(conversation_id, dialog.input.text()))
        dialog.deleteLater()

    @Slot()
    def _rename_current(self) -> None:
        if self._controller is not None and self._controller.state.conversation_id is not None:
            self.rename_conversation(self._controller.state.conversation_id)

    @Slot(str)
    def _pin(self, conversation_id: str) -> None:
        self._catalog.change(lambda: self._catalog.service.toggle_pin(conversation_id))

    @Slot(str)
    def _archive(self, conversation_id: str) -> None:
        try:
            needs_stop = self._catalog.archive_requires_stop(conversation_id)
        except (OSError, ValueError) as exc:
            self._route_error(str(exc))
            return
        if needs_stop:
            dialog = ArchiveConfirmation(self)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            dialog.deleteLater()
            if not accepted:
                return
        self._catalog.archive(conversation_id, stop_confirmed=needs_stop)

    def _clear_current(self) -> None:
        controller, self._controller = self._controller, None
        self.chat.clear()
        if controller is not None:
            controller.state_changed.disconnect(self._show_current)
            controller.conversation_changed.disconnect(self._show_current)
            self._hub.release_view(controller)
        self.sidebar.set_active(None)
        self.sidebar.set_new_enabled(True)
        self._heading.setText("主界面")
        self._route_error("")
        self._start_visible = True
        self.start.set_scope(None)
        self._pages.setCurrentWidget(self.start)
        self._show_current()

    def close_block(self) -> CloseBlock | None:
        self.splitter.flush_preferences()
        blocks = []
        for composer in (self.chat.composer, self.start.composer):
            if composer.page_commands is not None:
                block = composer.page_commands.command_source.close_block()
                if block is not None:
                    blocks.append(block)
        if blocks:
            return blocks[0]
        return self.start.close_block() or self._catalog.close_block()

    def invalidate_skill_commands(self):
        for composer in (self.chat.composer, self.start.composer):
            if composer.page_commands is not None:
                composer.page_commands.invalidate_source()
