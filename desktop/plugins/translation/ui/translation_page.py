"""通过平台动作检测、准备翻译项目，并只读展示 Core 返回的文本。"""
import json
from collections import Counter
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

from ....bootstrap import create_runtime
from ....application.tasks import TaskRegistry
from ....runtime.runs import RunEvent, RunState, exception_diagnostics
from ....ui.contracts import CloseBlock
from ....ui.task_bridge import ActionWorker
from .. import PrepareResult, ProjectSnapshot
from .project_prepare_panel import ProjectPreparePanel


TASK_LABELS = {
    "NONE": "未翻译", "PROCESSED": "已处理",
    "NEEDS_HUMAN": "待人工确认", "ERROR": "错误",
}
ACTION_LABELS = {
    "open_project": "打开项目", "detect_game": "检测游戏", "prepare_project": "准备项目",
}


class TranslationPage(QWidget):
    """加载成功后替换当前展示；失败时保留原有快照和选择。"""

    def __init__(self, settings_path: Path) -> None:
        super().__init__()
        # 页面工厂沿用配置路径参数；检测、准备和浏览不需要读取模型配置。
        self._settings_path = settings_path
        self._worker: ActionWorker | None = None
        self._task_registry = TaskRegistry()
        self._active_run_id: str | None = None
        self._active_payload: dict[str, Any] = {}
        self._project: ProjectSnapshot | None = None
        self._scenes: dict[str, dict[str, Any]] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._entries: dict[str, dict[str, Any]] = {}
        self.setWindowTitle("游戏翻译")
        self._build_form()

    def close_block(self) -> CloseBlock | None:
        """停止请求不等于退出；等进程和线程实际回收后才能关闭。"""
        if self._worker is not None:
            return CloseBlock("后台任务尚未结束", "请先停止操作，等待后台退出后关闭窗口。")
        return None

    def bind_tasks(self, tasks: TaskRegistry) -> None:
        """挂载时接收宿主登记表；独立页面仍有自己的单页登记表。"""
        if self._worker is not None:
            raise ValueError("运行期间不能更换任务登记表。")
        self._task_registry = tasks

    def _build_form(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("打开已有翻译项目，或从游戏目录准备项目，查看文本和来源。"))
        inputs = QTabWidget()
        inputs.addTab(self._create_path_controls(), "打开已有项目")
        self._prepare_panel = ProjectPreparePanel()
        self._prepare_panel.detect_requested.connect(self._on_detect)
        self._prepare_panel.prepare_requested.connect(self._on_prepare)
        inputs.addTab(self._prepare_panel, "从游戏准备项目")
        layout.addWidget(inputs)
        self._status_label = QLabel("尚未打开项目")
        self._status_label.setWordWrap(True)
        self._status_label.setTextFormat(Qt.TextFormat.PlainText)
        self._summary_label = QLabel()
        self._summary_label.setWordWrap(True)
        self._summary_label.setTextFormat(Qt.TextFormat.PlainText)
        status_row = QHBoxLayout()
        status_row.addWidget(self._status_label, 1)
        self._stop_button = QPushButton("停止")
        self._stop_button.setVisible(False)
        self._stop_button.clicked.connect(self._stop_action)
        status_row.addWidget(self._stop_button)
        layout.addLayout(status_row)
        layout.addWidget(self._summary_label)
        lists = QSplitter(Qt.Orientation.Horizontal)
        lists.addWidget(self._create_scene_panel())
        lists.addWidget(self._create_task_panel())
        lists.setStretchFactor(1, 2)
        views = QSplitter(Qt.Orientation.Vertical)
        views.addWidget(lists)
        views.addWidget(self._create_details())
        layout.addWidget(views)

    def _create_path_controls(self) -> QWidget:
        panel = QWidget()
        layout = QHBoxLayout(panel)
        self._work_dir_input = QLineEdit()
        self._work_dir_input.setPlaceholderText("包含 project.json 的翻译工作目录")
        self._browse_button = QPushButton("选择目录")
        self._open_button = QPushButton("打开项目")
        layout.addWidget(self._work_dir_input)
        layout.addWidget(self._browse_button)
        layout.addWidget(self._open_button)
        self._browse_button.clicked.connect(self._on_browse)
        self._open_button.clicked.connect(self._on_open)
        return panel

    def _create_scene_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("Scene（分组不一定是连续剧情）"))
        self._scene_list = QListWidget()
        self._scene_list.currentItemChanged.connect(self._on_scene_selected)
        layout.addWidget(self._scene_list)
        return panel

    def _create_task_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self._hide_processed = QCheckBox("隐藏已处理任务")
        self._hide_processed.toggled.connect(self._render_tasks)
        self._task_count_label = QLabel("选择 Scene 后查看文本")
        layout.addWidget(self._hide_processed)
        layout.addWidget(self._task_count_label)
        self._task_table = QTableWidget(0, 5)
        self._task_table.setHorizontalHeaderLabels(["状态", "类型", "发言者", "原文", "译文"])
        self._task_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._task_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._task_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._task_table.verticalHeader().setVisible(False)
        header = self._task_table.horizontalHeader()
        for column in range(3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for column in (3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        self._task_table.itemSelectionChanged.connect(self._on_task_selected)
        layout.addWidget(self._task_table)
        return panel

    def _create_details(self) -> QTabWidget:
        tabs = QTabWidget()
        self._source_view = QPlainTextEdit()
        self._translation_view = QPlainTextEdit()
        self._location_view = QPlainTextEdit()
        self._scene_view = QPlainTextEdit()
        self._diagnostic_view = QPlainTextEdit()
        for widget, title in (
            (self._source_view, "原文"), (self._translation_view, "已有译文"),
            (self._location_view, "文本来源"), (self._scene_view, "Scene 信息"),
            (self._diagnostic_view, "错误定位"),
        ):
            widget.setReadOnly(True)
            tabs.addTab(widget, title)
        return tabs

    @Slot()
    def _on_browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择翻译工作目录", self._work_dir_input.text(),
        )
        if directory:
            self._work_dir_input.setText(directory)

    @Slot()
    def _on_open(self) -> None:
        """固定本次路径，使用没有模型后端的 Runtime 启动一次只读动作。"""
        directory = self._work_dir_input.text().strip()
        if not directory:
            self._status_label.setText("请先选择或填写翻译工作目录。")
            return
        self._start_action("open_project", {"work_dir": directory})

    @Slot(object)
    def _on_detect(self, payload: dict[str, Any]) -> None:
        self._start_action("detect_game", payload)

    @Slot(object)
    def _on_prepare(self, payload: dict[str, Any]) -> None:
        self._start_action("prepare_project", payload)

    def _start_action(self, action: str, payload: dict[str, Any]) -> None:
        """同一页面串行执行 Core 操作，三种入口共用忙碌状态和线程连接。"""
        if self._worker is not None:
            return
        runtime = create_runtime()
        worker = ActionWorker(runtime, "translation", action, payload, self)
        target = payload.get("work_dir", payload.get("game_dir", ""))
        try:
            self._task_registry.register(worker.run_id, "translation:" + action, None,
                                         worker.request_cancel, workspace=Path(target))
        except (OSError, ValueError) as exc:
            worker.deleteLater()
            self._status_label.setText(str(exc))
            return
        self._worker = worker
        self._active_run_id = worker.run_id
        self._active_payload = dict(payload)
        worker.progress.connect(self._on_run_event, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._on_thread_finished, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(worker.deleteLater)
        self._set_busy(True)
        self._status_label.setText(f"正在{ACTION_LABELS[action]}：{target}")
        self._diagnostic_view.clear()
        try:
            worker.start()
        except Exception as exc:
            # 启动失败还未进入 Core，沿现有错误显示和按钮恢复路线收尾。
            self._on_run_event(RunEvent(
                worker.run_id, "translation", action, RunState.FAILED,
                error="后台线程未能启动，本次没有执行 Core。", diagnostics=exception_diagnostics(exc),
            ))
            self._finish_worker(worker)
            worker.deleteLater()

    @Slot()
    def _stop_action(self) -> None:
        worker = self._worker
        if worker is not None and self._task_registry.cancel(worker.run_id):
            self._stop_button.setEnabled(False)
            self._status_label.setText("正在停止操作；等待后台退出，日志和已生成文件保留。")

    @Slot(object)
    def _on_run_event(self, event: RunEvent) -> None:
        if event.run_id != self._active_run_id:
            return
        if event.state != RunState.RUNNING:
            self._task_registry.update_phase(event.run_id, event.state.value)
        if event.state == RunState.FAILED:
            suffix = "；仍显示之前打开的项目。" if self._project is not None else ""
            target = self._active_payload.get("work_dir", self._active_payload.get("game_dir", ""))
            self._status_label.setText(f"{ACTION_LABELS[event.action]}未完成（{target}）：{event.error}{suffix}")
            self._diagnostic_view.setPlainText(event.diagnostics or "")
            if event.diagnostics:
                self._status_label.setText(self._status_label.text() + " 位置见下方错误定位。")
            if event.action == "detect_game":
                self._prepare_panel.show_detection_error()
        elif event.state == RunState.SUCCEEDED:
            if event.action == "detect_game":
                self._prepare_panel.show_detection(self._active_payload["game_dir"], event.result)
                self._status_label.setText("检测已完成；判断依据见准备面板。")
            elif event.action == "prepare_project":
                self._show_prepare_result(event.result)
            else:
                self._show_project(event.result)
                self._status_label.setText(f"已打开：{self._project.work_dir}")
        elif event.state == RunState.CANCELLED:
            self._status_label.setText("本次操作被取消；请核对工作目录，已有展示保留。")

    def _show_prepare_result(self, result: PrepareResult) -> None:
        """区分未更新报告与项目快照，只有后者可以替换当前展示。"""
        report = result.report
        if result.project is None:
            warnings = "\n".join(str(item) for item in report.get("warnings", []))
            message = report.get("note", "本次没有创建或更新项目。")
            if warnings:
                message += "\n" + warnings
            if self._project is not None:
                message += "\n仍显示之前打开的项目。"
            self._status_label.setText(message)
            return
        self._show_project(result.project)
        self._work_dir_input.setText(str(result.project.work_dir))
        self._status_label.setText(
            f"已准备：{result.project.work_dir}；文本位置 {report['entries']}；任务 {report['tasks']}。"
        )

    def _set_busy(self, busy: bool) -> None:
        self._work_dir_input.setEnabled(not busy)
        self._open_button.setEnabled(not busy)
        self._browse_button.setEnabled(not busy)
        self._prepare_panel.set_busy(busy)
        self._stop_button.setVisible(busy)
        self._stop_button.setEnabled(busy)

    @Slot()
    def _on_thread_finished(self) -> None:
        self._finish_worker(self.sender())

    def _finish_worker(self, worker: ActionWorker) -> None:
        if worker is not self._worker:
            return
        self._task_registry.release(worker.run_id)
        self._worker = None
        self._active_run_id = None
        self._set_busy(False)

    def _show_project(self, project: ProjectSnapshot) -> None:
        """沿 Core 的 ID 建立展示索引，不重新划分 Scene 或生成 Task。"""
        self._project = project
        self._scenes = {scene["id"]: scene for scene in project.manifest["scenes"]}
        self._tasks = {task["id"]: task for task in project.tasks}
        self._entries = {entry["task_id"]: entry for entry in project.entries}
        self._scene_list.blockSignals(True)
        self._scene_list.clear()
        for scene in project.manifest["scenes"]:
            title = f"{scene['title']} · {scene['kind']} · {len(scene['task_ids'])} 条"
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, scene["id"])
            self._scene_list.addItem(item)
        self._scene_list.blockSignals(False)
        counts = Counter(task["status"] for task in project.tasks)
        labels = "；".join(f"{TASK_LABELS[state]} {count}" for state, count in counts.items())
        warnings = "\n".join(str(warning) for warning in project.manifest.get("warnings", []))
        self._summary_label.setText(
            f"引擎：{project.manifest['engine']['family']}；Scene {len(self._scenes)}；"
            f"文本位置 {len(project.entries)}；任务 {len(project.tasks)}\n{labels}"
            + (f"\nCore 提示：{warnings}" if warnings else "")
        )
        if self._scene_list.count():
            self._scene_list.setCurrentRow(0)
        else:
            self._render_tasks()

    @Slot()
    def _on_scene_selected(self) -> None:
        self._render_tasks()

    @Slot()
    def _render_tasks(self) -> None:
        """只筛选当前视图；顺序沿用 Scene.task_ids，Task 状态不变。"""
        item = self._scene_list.currentItem()
        if item is None:
            self._fill_task_rows([])
            self._scene_view.clear()
            self._task_count_label.setText("选择 Scene 后查看文本")
            return
        scene_id = item.data(Qt.ItemDataRole.UserRole)
        scene = self._scenes[scene_id]
        tasks = [self._tasks[task_id] for task_id in scene["task_ids"]]
        visible = tasks
        if self._hide_processed.isChecked():
            visible = [task for task in tasks if task["status"] != "PROCESSED"]
        self._fill_task_rows(visible)
        self._task_count_label.setText(f"当前显示 {len(visible)} / {len(tasks)} 条任务")
        metadata = {
            key: scene[key] for key in ("id", "title", "kind", "source_context") if key in scene
        }
        self._scene_view.setPlainText(json.dumps(metadata, ensure_ascii=False, indent=2))

    def _fill_task_rows(self, tasks: list[dict[str, Any]]) -> None:
        """填充只读表格；刷新期间暂停选择通知，填完再选中第一条。"""
        self._task_table.blockSignals(True)
        self._task_table.setRowCount(0)
        self._task_table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            values = (
                TASK_LABELS[task["status"]], task["type"], task.get("speaker", ""),
                task["text"], task["translation"],
            )
            for column, value in enumerate(values):
                cell = QTableWidgetItem("" if value is None else str(value))
                if column == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, task["id"])
                self._task_table.setItem(row, column, cell)
        self._task_table.blockSignals(False)
        self._clear_task_details()
        if tasks:
            self._task_table.setCurrentCell(0, 0)

    @Slot()
    def _on_task_selected(self) -> None:
        row = self._task_table.currentRow()
        cell = self._task_table.item(row, 0) if row >= 0 else None
        if cell is None:
            self._clear_task_details()
            return
        task_id = cell.data(Qt.ItemDataRole.UserRole)
        task, entry = self._tasks[task_id], self._entries[task_id]
        self._source_view.setPlainText(task["text"])
        self._translation_view.setPlainText(task["translation"])
        location = {key: entry[key] for key in (
            "entry_id", "task_id", "file", "asset", "path", "format", "type", "text_locale", "source_context", "structure",
        ) if key in entry}
        self._location_view.setPlainText(json.dumps(location, ensure_ascii=False, indent=2))

    def _clear_task_details(self) -> None:
        self._source_view.clear()
        self._translation_view.clear()
        self._location_view.clear()
