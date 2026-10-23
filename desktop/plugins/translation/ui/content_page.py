"""场景全文双栏对照；页面只读取Core，不提供译文编辑或模型发送入口。"""
from pathlib import Path
from dataclasses import replace

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout

from .. import _load_snapshot
from .parallel_text import ParallelTextView, scene_pairs
from ....ui.common.background_read import BackgroundRead
from ....ui.common.buttons import MotionButton
from ....ui.common.popup import CardPopup
from ....ui.common.surfaces import WhiteSurface
from ....ui.common.widgets import ClickComboBox, IconButton
from ....ui.shell import theme


class TranslationContentPage(WhiteSurface):
    def __init__(self, context, remember):
        super().__init__()
        self.setProperty("minimalPageHeader", True)
        self._context = context
        self._remember = remember
        self._root = None
        self._pending_root = self._workspace_project(context.workspace)
        self._snapshot = None
        self._dirty = True
        self._generation = 0
        self._reading = None
        self._scene_id = None
        self._pending_scene = None
        self._job = BackgroundRead(self)
        self._job.completed.connect(self._received)
        self._job.failed.connect(self._failed)
        self._job.settled.connect(self._settled)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        row = QHBoxLayout()
        row.setContentsMargins(16, 10, 12, 8)
        row.setSpacing(6)
        self.scene = ClickComboBox(compact=True)
        self.scene.setObjectName("translationScene")
        self.scene.setAccessibleName("切换翻译场景")
        self.scene.setMaximumWidth(360)
        self.scene.setStyleSheet(theme.choice_stylesheet(compact=True) + f"""
            QComboBox#translationScene {{ background: transparent; border: none;
                padding: 4px 28px 4px 10px; min-height: 18px; }}
            QComboBox#translationScene:hover, QComboBox#translationScene:focus,
            QComboBox#translationScene[expanded="true"] {{ background: {theme.CONTROL}; }}
        """)
        self.scene.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.scene.setSizeAdjustPolicy(ClickComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.scene.setMinimumContentsLength(4)
        self.scene.hide()
        self.scene.currentIndexChanged.connect(self._show_scene)
        self._more = IconButton("more", "翻译项目操作", self)
        self._more.clicked.connect(self._toggle_menu)
        self._expand = IconButton("expand", "展开或还原窗口", self)
        self._expand.clicked.connect(self._toggle_expanded)
        row.addWidget(self.scene, 1)
        row.addStretch(1)
        row.addWidget(self._more)
        row.addWidget(self._expand)
        layout.addLayout(row)
        self._notice = QLabel()
        self._notice.setWordWrap(True)
        self._notice.setTextFormat(Qt.TextFormat.PlainText)
        self._notice.setStyleSheet(f"color: {theme.ERROR}; padding: 4px 18px 8px;")
        self._notice.hide()
        layout.addWidget(self._notice)
        self.reader = ParallelTextView(self)
        self.reader.setAccessibleName("左侧原文，右侧译文")
        layout.addWidget(self.reader, 1)
        self._menu = CardPopup(self._more, "translationMenu")
        self._menu.bind_toggle(self._toggle_menu)
        menu = QVBoxLayout(self._menu)
        menu.setContentsMargins(12, 12, 12, 12)
        menu.setSpacing(2)
        self._project_label = QLabel()
        self._project_label.setTextFormat(Qt.TextFormat.PlainText)
        self._project_label.setWordWrap(True)
        self._project_label.setMaximumWidth(300)
        self._project_label.setStyleSheet(f"color: {theme.MUTED}; padding: 6px 10px;")
        menu.addWidget(self._project_label)
        choose = MotionButton("选择翻译项目…", variant="ghost", size="medium")
        choose.setStyleSheet("text-align: left; padding: 6px 10px;")
        choose.clicked.connect(self._choose)
        menu.addWidget(choose)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(160)
        self._timer.timeout.connect(self.refresh)
        self._set_empty()

    @staticmethod
    def _workspace_project(workspace):
        if not workspace:
            return None
        path = Path(workspace).expanduser().resolve()
        return str(path) if (path / "project.json").is_file() else None

    def bind_context(self, context):
        changed = context.conversation_id != self._context.conversation_id
        if context.conversation_id is None and self._context.conversation_id is None:
            changed = (context.project_id, context.workspace) != (self._context.project_id, self._context.workspace)
        if changed:
            # 显式在另一对话打开页面才切换；独立窗口不跟随普通聊天导航。
            self._generation += 1
            self._root = self._pending_root = None
            self._snapshot = None
            self._scene_id = self._pending_scene = None
            self.scene.clear()
            self.reader.set_pairs(())
            self._notice.hide()
        self._context = context
        if context.resource_id is not None:
            if not context.workspace:
                raise ValueError("查看翻译引用需要原工作区。")
            self.bind_selection(context.conversation_id, context.workspace, context.resource_id)
            return
        if self._root is None and self._pending_root is None:
            self._pending_root = self._workspace_project(context.workspace)
            self._dirty = True
        self._set_empty()

    @property
    def conversation_id(self):
        return self._context.conversation_id

    def show_notice(self, text):
        self._notice.setText(text)
        self._notice.show()

    def restore_selection(self, root, scene_id):
        """恢复经过校验后保存的用户选择；目录失效也保留原路径供重新选择。"""
        self._request_project(root, scene_id)

    def bind_selection(self, conversation_id, workspace, work_dir):
        """工具引用不是权限；链接失效或越界时保留原内容并显示原因。"""
        try:
            allowed = Path(workspace).resolve(strict=True)
            root = (allowed / work_dir).resolve(strict=True)
            if not root.is_relative_to(allowed) or not root.is_dir():
                raise ValueError("翻译项目不在当前工作区内，或目录无效。")
        except (OSError, ValueError, RuntimeError) as exc:
            self.show_notice(f"无法选择翻译项目：{exc}")
            return
        self.bind_context(replace(self._context, conversation_id=conversation_id,
                                  workspace=str(allowed), resource_id=None))
        self._request_project(str(root))

    def _request_project(self, root, scene_id=None):
        self._generation += 1
        self._pending_root = root
        self._pending_scene = scene_id
        self._notice.hide()
        self._dirty = True
        self._set_empty()
        if self.isVisible():
            self.refresh()

    def close_block(self):
        return self._job.close_block()

    def invalidate(self, root):
        # 主运行的工作区可包含翻译项目；审核后成功提交沿原exclusive通知刷新。
        selected = self._pending_root or self._root
        if selected and Path(selected).resolve().is_relative_to(Path(root).resolve()):
            self._dirty = True
            if self.isVisible():
                self._timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        # 再次打开时重读已保存内容；没有后台轮询，也不增加模型请求。
        self._dirty = True
        if self._dirty:
            self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        self._menu.hide()
        super().hideEvent(event)

    def _toggle_menu(self):
        if self._menu.is_open:
            self._menu.dismiss(restore_focus=True)
            return
        self._project_label.setText(self._pending_root or self._root or "尚未选择翻译项目")
        self._menu.adjustSize()
        self._menu.resize(max(230, self._menu.width()), self._menu.height())
        anchor = self._more.mapToGlobal(QPoint(self._more.width(), self._more.height() + 2))
        area = self.screen().availableGeometry()
        self._menu.move(max(area.left() + 4, anchor.x() - self._menu.width()),
                        min(anchor.y(), area.bottom() - self._menu.height() - 4))
        self._menu.present()

    def _toggle_expanded(self):
        window = self.window()
        if window.isMaximized():
            window.showNormal()
        else:
            window.showMaximized()

    def _choose(self):
        self._menu.dismiss()
        root = QFileDialog.getExistingDirectory(self, "选择已有翻译项目", self._pending_root or self._root or "")
        if root:
            # 选错目录不丢失原快照，等新目录通过Core校验后再提交显示。
            self._request_project(str(Path(root).resolve()))

    def refresh(self):
        if self._job.worker is not None:
            self._dirty = True
            return
        root = self._pending_root or self._root
        if not root:
            return
        generation = self._generation
        self._dirty = False
        self._reading = generation, root
        workspace = self._context.workspace if self._pending_root or self._root else None
        def read():
            # Agent选择必须检查固定项目文件的真实路径；手动目录仍允许用户显式选取外部项目。
            if workspace and Path(root).is_relative_to(Path(workspace)):
                from ..project_access import load_workspace_project
                snapshot = load_workspace_project(Path(root).relative_to(Path(workspace)).as_posix(), Path(workspace))
            else:
                snapshot = _load_snapshot(Path(root))
            return generation, root, snapshot
        self._job.start(read)

    def _received(self, result):
        generation, root, snapshot = result
        if (generation, root) != (self._generation, self._pending_root or self._root):
            return
        changed = root != self._root
        selected = self._pending_scene
        if selected is None and not changed:
            selected = self.scene.currentData()
        self._root = root
        self._pending_root = None
        self._pending_scene = None
        if changed:
            self._scene_id = None
        self._snapshot = snapshot
        self._notice.hide()
        self.scene.blockSignals(True)
        self.scene.clear()
        for scene in snapshot.manifest["scenes"]:
            self.scene.addItem(scene["title"], scene["id"])
        self.scene.setCurrentIndex(max(0, self.scene.findData(selected)))
        self.scene.blockSignals(False)
        self.scene.setToolTip(root)
        self.scene.setEnabled(bool(self.scene.count()))
        self.scene.setVisible(bool(self.scene.count()))
        self._show_scene()

    def _failed(self, message):
        if self._reading != (self._generation, self._pending_root or self._root):
            return
        root = self._pending_root or self._root
        self.show_notice(f"读取失败：{root}\n{message}" + ("；当前内容保留。" if self._snapshot else ""))
        if self._snapshot is None:
            self.reader.set_empty_text("请从右上角菜单重新选择翻译项目。")

    def _settled(self):
        self._reading = None
        if self._dirty and self.isVisible():
            self._timer.start()

    def _set_empty(self):
        if self._snapshot is None:
            self.scene.setEnabled(False)
            self.scene.hide()
            self.reader.set_empty_text("正在读取…" if self._pending_root or self._root else "请让 Agent 选择翻译项目，或从右上角 ⋯ 手动选择")

    def _show_scene(self, *_):
        if self._snapshot is None:
            return
        scene_id = self.scene.currentData()
        pairs = scene_pairs(self._snapshot, scene_id)
        self.reader.set_empty_text("这个场景没有文本。")
        self.reader.set_pairs(pairs, preserve=scene_id == self._scene_id)
        self._scene_id = scene_id
        if self._pending_root == self._root:
            # 同项目重读期间的手动切换优先于打开页面时的旧Scene。
            self._pending_scene = scene_id
        if self._pending_root is None:
            self._remember(self.conversation_id, self._root, self._context.workspace, scene_id)
