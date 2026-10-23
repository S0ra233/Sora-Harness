"""统一承载插件侧栏与非模态窗口；页面内容和执行状态留在插件。"""
from dataclasses import replace
import logging

from PySide6.QtCore import QAbstractAnimation, QEvent, QObject, Property, QEasingCurve, QPropertyAnimation, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLayout, QStackedWidget, QVBoxLayout, QWidget
from shiboken6 import isValid

from .surfaces import WhiteSurface
from .widgets import ElidedLabel, IconButton
from .focus import FocusReturn
from ..shell import theme
from ..contracts import CloseBlock, UiContribution, release_idle_contribution


class PluginPageFrame(WhiteSurface):
    closed = Signal()

    def __init__(self, title, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        # 侧页缩窄时裁切内容，布局的最小宽度不能反过来阻止关闭动画。
        outer.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.header = QWidget(self)
        row = QHBoxLayout(self.header)
        row.setContentsMargins(12, 10, 12, 10)
        self.title = ElidedLabel(title)
        self.title.setStyleSheet(f"background: {theme.BACKGROUND}; border: 1px solid {theme.BORDER}; border-radius: {theme.BUTTON_RADIUS}px; padding: 8px 12px;")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        close = IconButton("close", "关闭辅助页面")
        close.clicked.connect(self.closed.emit)
        row.addWidget(self.title, 1)
        row.addWidget(close)
        outer.addWidget(self.header)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)

    def minimumSizeHint(self):
        return QSize(0, 0)


class _PluginWindow(QDialog):
    def __init__(self, title, parent, frame, contribution):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle(title)
        self.resize(1000, 720)
        self.setMinimumSize(660, 440)
        self.setObjectName("pluginWindow")
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(theme.BACKGROUND))
        self.setPalette(palette)
        self.setAutoFillBackground(True)
        self.setFont(parent.font())
        self.setStyleSheet(theme.shell_stylesheet() + f"QDialog#pluginWindow {{ background: {theme.BACKGROUND}; }}")
        self._caption_ready = False
        self.focus_return = FocusReturn(self)
        self.contribution = contribution
        # 简洁内容页已有自己的工具行；独立窗口仍有系统标题和关闭入口。
        frame.header.setVisible(not bool(contribution.widget.property("minimalPageHeader")))
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(frame)
        frame.closed.connect(self.close)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._caption_ready:
            self._caption_ready = True
            from ..shell.windows_caption import white_rounded_caption
            try:
                failed = white_rounded_caption(int(self.winId()))
                if failed:
                    logging.getLogger(__name__).warning("辅助窗口部分原生外观属性不受支持：%s", failed)
            except OSError as exc:
                logging.getLogger(__name__).warning("辅助窗口原生外观设置失败：%s", type(exc).__name__)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.focus_return.restore()

    def closeEvent(self, event):
        # 辅助窗口隐藏而非销毁；原选择与读取线程的所有者都保持有效。
        event.accept()


class PageHost(QObject):
    aside_changed = Signal(object)

    def __init__(self, registry, settings, services, parent, aside_splitter, context):
        super().__init__(parent)
        self.registry, self.settings, self.services = registry, settings, services
        self._plugin_defaults = getattr(registry, "plugin_defaults", {})
        self.owner, self.splitter, self.context = parent, aside_splitter, context
        self._disposed = False
        self._instances = {}
        self._failed_instances = {}
        self._windows = {}
        self._aside_pages = {}
        self._active = None
        self._bound_context = None
        self._width = None
        self._extent = 0
        self._focus_return = FocusReturn(self)
        self._restore_focus = False
        self._drag_sizes = None
        self._drag_cancelled = False
        self._drag_moved = False
        self._motion = QPropertyAnimation(self, b"extent", self)
        self._motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._motion.finished.connect(self._settled)
        self.aside = PluginPageFrame("辅助页面", parent)
        self.splitter.addWidget(self.aside)
        self._handle = self.splitter.handle(1)
        self.splitter.setHandleWidth(6)
        self._handle.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._handle.setAccessibleName("调整聊天与辅助页面宽度")
        self._handle.setToolTip("左右键调整宽度；双击或Home复位；拖动时Esc还原")
        self.splitter.installEventFilter(self)
        self._handle.installEventFilter(self)
        self.aside.hide()
        self.aside.closed.connect(self.close_aside)
        self.splitter.splitterMoved.connect(self._dragged)
        self.splitter.destroyed.connect(self._dispose)
        self.aside.destroyed.connect(self._dispose)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._dispose)

    def _dispose(self, *_args):
        if self._disposed:
            return
        self._disposed = True
        self._active = None
        if isValid(self._motion):
            self._motion.stop()
        for target in (self.splitter, self._handle):
            if isValid(target) and isValid(self):
                target.removeEventFilter(self)
        for contribution in (*self._instances.values(), *self._failed_instances.values()):
            if contribution.dispose:
                try:
                    contribution.dispose()
                except Exception as exc:
                    logging.getLogger(__name__).error("插件页面释放失败：页面=%s 错误类型=%s", contribution.contribution_id, type(exc).__name__)

    @Property(int)
    def extent(self):
        return self._extent

    @extent.setter
    def extent(self, value):
        if self._disposed:
            return
        self._extent = max(0, value)
        self.aside.setMaximumWidth(max(1, self._extent))
        total = self._available_width()
        self.splitter.setSizes([max(0, total - self._extent), self._extent])

    def _available_width(self):
        return max(0, self.splitter.contentsRect().width() - self.splitter.handleWidth())

    def _clamp_width(self, width):
        return max(theme.PANEL_MIN_WIDTH, min(width, self._available_width() - theme.PANEL_MIN_CHAT_WIDTH))

    def _resize_aside(self, width):
        self._motion.stop()
        self.aside.setMaximumWidth(16777215)
        width = self._clamp_width(width)
        self.splitter.setSizes([self._available_width() - width, width])
        self._extent = self.aside.width()

    def _dragged(self, *_):
        if self._disposed:
            return
        if self._active and self._drag_sizes is not None and not self._drag_cancelled:
            self._extent = self.aside.width()

    def eventFilter(self, watched, event):
        if self._disposed or not isValid(self.splitter) or not isValid(self.aside):
            return False
        kind = event.type()
        if watched is self._handle and self._active is not None:
            if kind == QEvent.Type.MouseButtonDblClick and event.button() == Qt.MouseButton.LeftButton:
                self._drag_sizes = None
                self._resize_aside(round(self._available_width() * theme.PANEL_DEFAULT_RATIO))
                self._width = self.aside.width()
                return True
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self._motion.stop()
                self.aside.setMaximumWidth(16777215)
                self._extent = self.aside.width()
                self._drag_sizes = self.splitter.sizes()
                self._drag_origin = event.globalPosition().toPoint()
                self._drag_cancelled = False
                self._drag_moved = False
                self._handle.setFocus(Qt.FocusReason.MouseFocusReason)
            elif kind == QEvent.Type.MouseMove and self._drag_sizes is not None:
                if self._drag_cancelled or (not self._drag_moved and (event.globalPosition().toPoint() - self._drag_origin).manhattanLength() < 4):
                    return True
                self._drag_moved = True
            elif kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                if self._drag_sizes is not None and not self._drag_cancelled and self._drag_moved:
                    self._resize_aside(self.aside.width())
                    self._width = self.aside.width()
                self._drag_sizes = None
            elif kind == QEvent.Type.ShortcutOverride and event.key() == Qt.Key.Key_Escape and self._drag_sizes is not None:
                event.accept()
                return True
            elif kind == QEvent.Type.KeyPress:
                if event.key() == Qt.Key.Key_Escape and self._drag_sizes is not None:
                    self._drag_cancelled = True
                    self.splitter.setSizes(self._drag_sizes)
                    self._extent = self.aside.width()
                    return True
                if event.key() in {Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Home}:
                    step = theme.PANEL_KEY_STEP * (4 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1)
                    width = (round(self._available_width() * theme.PANEL_DEFAULT_RATIO) if event.key() == Qt.Key.Key_Home
                             else self.aside.width() + (step if event.key() == Qt.Key.Key_Left else -step))
                    self._resize_aside(width)
                    self._width = self.aside.width()
                    return True
        if watched is self.splitter and event.type() == QEvent.Type.Resize and self._active is not None:
            if (event.size().width() < theme.PANEL_SPLIT_MIN_WIDTH) != (event.oldSize().width() < theme.PANEL_SPLIT_MIN_WIDTH):
                self._show_aside()
            elif event.size().width() >= theme.PANEL_SPLIT_MIN_WIDTH and self._motion.state() != QAbstractAnimation.State.Running:
                self._resize_aside(self._width or round(self._available_width() * theme.PANEL_DEFAULT_RATIO))
        return super().eventFilter(watched, event)

    def _settled(self):
        if self._disposed:
            return
        if self._active is None:
            self.aside.hide()
            self.splitter.widget(0).show()
            if self._restore_focus:
                self._focus_return.restore()
                self._restore_focus = False
        else:
            self.aside.setMaximumWidth(16777215)

    def _show_aside(self):
        if self._disposed:
            return
        self._motion.stop()
        total = self._available_width()
        self.aside.show()
        if self.splitter.width() < theme.PANEL_SPLIT_MIN_WIDTH:
            self.splitter.widget(0).hide()
            self.aside.setMaximumWidth(16777215)
            self._extent = total
            return
        self.splitter.widget(0).show()
        end = self._clamp_width(self._width or round(total * theme.PANEL_DEFAULT_RATIO))
        self._motion.setDuration(max(1, round(theme.PANEL_ENTER_DURATION_MS * abs(end - self._extent) / max(1, end, self._extent))))
        self._motion.setStartValue(self._extent)
        self._motion.setEndValue(end)
        self._motion.start()

    def available(self, definition, context):
        if definition.requires_conversation and not context.conversation_id:
            return "请先打开已有工作会话。"
        if not self.settings.enabled(definition.plugin_id, default=self._plugin_defaults.get(definition.plugin_id, False)):
            try:
                permitted = definition.available_when_disabled is not None and definition.available_when_disabled(context)
            except Exception as exc:
                raise self._page_error(definition.page_id, exc) from exc
            if not permitted:
                return "请先在插件管理页启用此插件。"
        return None

    def open_page(self, page_id, context=None, resource_id=None):
        definition = next((item for item in self.registry.pages() if item.page_id == page_id), None)
        if definition is None:
            raise ValueError("插件页面尚未登记。")
        context = context or self.context()
        if resource_id is not None:
            context = replace(context, resource_id=resource_id)
        reason = self.available(definition, context)
        if reason:
            raise ValueError(reason)
        contribution = self._instances.get(page_id)
        fresh = contribution is None
        try:
            if fresh:
                contribution = self._failed_instances.pop(page_id, None) or definition.create_page(context)
                if (not isinstance(contribution, UiContribution) or not isinstance(contribution.widget, QWidget)
                        or not isValid(contribution.widget)):
                    raise TypeError("页面工厂须返回UiContribution及有效的QWidget。")
                if contribution.bind_tasks is not None:
                    contribution.bind_tasks(self.services.tasks)
            if contribution.bind_context is not None:
                contribution.bind_context(context)
        except Exception as exc:
            if self._active == page_id:
                self.close_aside()
            if page_id in self._windows:
                self._windows[page_id].hide()
            if fresh and isinstance(contribution, UiContribution):
                # 有活动任务的失败页面仍保留所有者及关闭检查，不销毁其线程。
                if not release_idle_contribution(contribution):
                    self._failed_instances[page_id] = contribution
            raise self._page_error(page_id, exc) from exc
        if fresh:
            self._instances[page_id] = contribution
        if definition.presentation == "aside":
            if self._active is None:
                self._focus_return.capture(self.owner)
            self._restore_focus = False
            if page_id not in self._aside_pages:
                self._aside_pages[page_id] = self.aside.stack.addWidget(contribution.widget)
            self._active = page_id
            self._bound_context = context
            self.aside.title.setText(definition.title)
            self.aside.title.setToolTip(definition.title)
            self.aside.stack.setCurrentIndex(self._aside_pages[page_id])
            self._show_aside()
            self.aside_changed.emit(page_id)
        else:
            window = self._windows.get(page_id)
            if window is None:
                frame = PluginPageFrame(definition.title)
                frame.stack.addWidget(contribution.widget)
                window = _PluginWindow(definition.title, self.owner.window(), frame, contribution)
                self._windows[page_id] = window
            if not window.isVisible():
                window.focus_return.capture(self.owner)
            window.show()
            window.raise_()
            window.activateWindow()
        return True

    def toggle_page(self, page_id):
        """标题按钮切换，正文资源与斜杠命令继续使用open_page定位。"""
        if self._active == page_id:
            self.close_aside()
            return
        self.open_page(page_id)

    def context_changed(self):
        if self._active is not None:
            context = self.context()
            if context == self._bound_context:
                return
            definition = next(item for item in self.registry.pages() if item.page_id == self._active)
            if definition.requires_conversation and context.conversation_id is None:
                self.close_aside()
                return
            contribution = self._instances[self._active]
            try:
                if contribution.bind_context:
                    contribution.bind_context(context)
            except Exception as exc:
                error = self._page_error(self._active, exc)
                self.close_aside()
                window = self.owner.window()
                if hasattr(window, "statusBar"):
                    window.statusBar().showMessage(str(error))
                return
            self._bound_context = context

    @staticmethod
    def _page_error(page_id, exc):
        logging.getLogger(__name__).error("插件页面绑定失败：页面=%s 错误类型=%s", page_id, type(exc).__name__)
        return ValueError(f"{page_id} 页面加载或绑定失败（{type(exc).__name__}），可以重新打开；其他页面不受影响。")

    def close_aside(self):
        if self._disposed:
            return
        moving = self._motion.state() == QAbstractAnimation.State.Running
        self._motion.stop()
        if self._active is not None and not moving and self.splitter.width() >= theme.PANEL_SPLIT_MIN_WIDTH:
            self._width = self.aside.width()
        focused = QApplication.focusWidget()
        self._restore_focus = self._restore_focus or focused is self._handle or focused is self.aside or focused is not None and self.aside.isAncestorOf(focused)
        self._drag_sizes = None
        self._active = None
        self._bound_context = None
        self.aside_changed.emit(None)
        if self.splitter.width() < theme.PANEL_SPLIT_MIN_WIDTH:
            self._extent = 0
            self._settled()
            return
        self._motion.setDuration(max(1, round(theme.PANEL_EXIT_DURATION_MS * self.aside.width() / max(1, self._width or self.aside.width()))))
        self._motion.setStartValue(self.aside.width())
        self._motion.setEndValue(0)
        self._motion.start()

    @property
    def active_page(self):
        return self._active

    def invalidate_project(self, root):
        for contribution in tuple(self._instances.values()):
            if contribution.invalidate:
                try:
                    contribution.invalidate(root)
                except Exception as exc:
                    logging.getLogger(__name__).error("插件刷新失败：页面=%s 错误类型=%s", contribution.contribution_id, type(exc).__name__)
                    window = self.owner.window()
                    if hasattr(window, "statusBar"):
                        window.statusBar().showMessage(f"{contribution.contribution_id} 刷新失败（{type(exc).__name__}），其他页面继续刷新。")

    def close_block(self):
        for contribution in (*self._instances.values(), *self._failed_instances.values()):
            if contribution.close_check:
                try:
                    block = contribution.close_check()
                except Exception as exc:
                    return CloseBlock("插件页面关闭检查失败", f"{contribution.contribution_id}：{type(exc).__name__}；请保留窗口并检查插件。")
                if block:
                    return block
        return None
