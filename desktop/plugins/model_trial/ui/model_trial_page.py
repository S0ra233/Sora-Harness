"""模型试运行页面，通过平台入口执行聊天或概括动作。"""
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Slot
from PySide6.QtGui import QShowEvent, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from ....bootstrap import create_runtime
from ....credentials import resolve_api_key
from ....attachments.store import ImageAttachment, ImageStore
from ....attachments.images import MAX_REQUEST_IMAGES
from ....llm.messages import user_message
from ....llm.models import ModelConfig
from ....llm.reasoning import available_efforts
from ....storage.paths import LocalPaths
from ....llm.validation import parse_max_output_tokens, parse_temperature
from ....runtime.runs import RunEvent, RunState, exception_diagnostics
from ....runtime.session import RunTracker, SessionStore
from ....settings import load_settings
from ....ui.contracts import CloseBlock
from ....ui.task_bridge import ActionWorker
from ....ui.conversations.worker import ConversationEvent, ConversationWorker
from .run_history_panel import RunHistoryPanel
from .attempt_view import format_replies


class ModelTrialPage(QWidget):
    """保存本页输入和当前运行引用；业务动作仍由 model_trial 插件处理。"""

    def __init__(self, settings_path: Path) -> None:
        super().__init__()
        self._settings_path = settings_path
        self._worker: ActionWorker | None = None
        self._active_run_id: str | None = None
        self._cancel_pending = False
        self._attachments = ImageStore(LocalPaths(settings_path.parent).attachments)
        self._images: dict[str, ImageAttachment] = {}
        self._image_worker: ConversationWorker | None = None
        self._effort_config: ModelConfig | None = None
        self.setWindowTitle("模型试运行")
        self._build_form()

    def close_block(self) -> CloseBlock | None:
        """待线程通知完成、结果落盘后才允许关闭；不在这里取消或写文件。"""
        if self._worker is not None or self._image_worker is not None:
            return CloseBlock("后台任务尚未结束", "请等待后台任务结束后关闭；模型请求可以先取消。")
        if self._runs.pending_count:
            return CloseBlock("结果尚未保存", "请到历史与请求页重试保存待存结果，再关闭窗口。")
        return None

    def _build_form(self) -> None:
        outer_layout = QVBoxLayout(self)
        tabs = QTabWidget()
        trial_form = QWidget()
        layout = QVBoxLayout(trial_form)
        store = SessionStore(LocalPaths(self._settings_path.parent).sessions)
        # 本页的结果是 ModelResponse，asdict 只负责转换，不参与记录的状态管理。
        self._runs = RunTracker(store, encode_result=asdict)
        self._history = RunHistoryPanel(self._runs)
        tabs.addTab(trial_form, "输入与运行")
        tabs.addTab(self._history, "历史与请求")
        outer_layout.addWidget(tabs)
        layout.addWidget(QLabel("使用设置中已启用接口的模型；密钥在请求开始时读取。"))
        form = QFormLayout()
        self._action_input = QComboBox()
        self._action_input.addItem("聊天", "chat")
        self._action_input.addItem("概括输入", "summarize")
        self._system_input = QPlainTextEdit()
        self._system_input.setPlaceholderText("可选的系统提示词")
        self._system_input.setMaximumHeight(90)
        self._prompt_input = QPlainTextEdit()
        self._prompt_input.setPlaceholderText("输入要发送给模型的内容")
        self._image_button = QPushButton("选择图片")
        self._clear_images_button = QPushButton("移除本次图片引用")
        self._image_label = QLabel("尚未选择图片；GIF／动画 WebP 只发送第一帧，原文件保留。")
        self._image_label.setTextFormat(Qt.TextFormat.PlainText)
        self._image_label.setWordWrap(True)
        self._temperature_input = QLineEdit()
        self._temperature_input.setPlaceholderText("留空时使用模型默认值")
        self._max_output_input = QLineEdit()
        self._max_output_input.setPlaceholderText("可选正整数；Messages 留空使用 4096，其他协议不发送")
        self._effort_input = QComboBox()
        self._effort_input.addItem("关闭强度设置（不发送 effort）", None)
        self._effort_input.setEnabled(False)
        form.addRow("动作", self._action_input)
        form.addRow("系统提示词", self._system_input)
        form.addRow("输入内容", self._prompt_input)
        form.addRow("图片附件", self._image_button)
        form.addRow(self._image_label)
        form.addRow(self._clear_images_button)
        form.addRow("温度", self._temperature_input)
        form.addRow("最大输出 token", self._max_output_input)
        form.addRow("推理档位", self._effort_input)
        layout.addLayout(form)
        self._stream_input = QCheckBox("逐段显示结果（需要接口支持流式响应）")
        layout.addWidget(self._stream_input)
        self._run_button = QPushButton("运行")
        self._cancel_button = QPushButton("取消")
        self._cancel_button.setEnabled(False)
        self._status_label = QLabel("尚未运行")
        self._status_label.setTextFormat(Qt.TextFormat.PlainText)
        self._status_label.setWordWrap(True)
        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        self._output.setPlaceholderText("模型结果显示在这里")
        self._details_label = QLabel()
        self._details_label.setWordWrap(True)
        layout.addWidget(self._run_button)
        layout.addWidget(self._cancel_button)
        layout.addWidget(self._status_label)
        layout.addWidget(self._output)
        layout.addWidget(self._details_label)
        self._run_button.clicked.connect(self._on_run)
        self._cancel_button.clicked.connect(self._on_cancel)
        self._image_button.clicked.connect(self._on_select_images)
        self._clear_images_button.clicked.connect(self._clear_images)

    @Slot()
    def _on_select_images(self) -> None:
        """只在 UI 线程选择路径；读取、解码和保存交给后台。"""
        if self._image_worker is not None:
            return
        filenames, _ = QFileDialog.getOpenFileNames(
            self, "选择输入图片", "", "图片 (*.png *.jpg *.jpeg *.gif *.webp);;所有文件 (*)",
        )
        if not filenames:
            return
        if len(filenames) + len(self._images) > MAX_REQUEST_IMAGES:
            self._status_label.setText("本次最多选择 8 张图片，请移除引用或减少选择。")
            return
        paths = [Path(name) for name in filenames]

        async def import_images(_):
            return await self._attachments.import_files(paths)

        worker = ConversationWorker(import_images, self)
        worker.progress.connect(self._on_image_event)
        worker.finished.connect(self._on_image_finished)
        self._image_worker = worker
        self._image_button.setEnabled(False)
        self._clear_images_button.setEnabled(False)
        self._run_button.setEnabled(False)
        self._status_label.setText("正在保存图片附件")
        try:
            worker.start()
        except Exception as exc:
            self._on_image_finished()
            self._status_label.setText(f"附件任务 {worker.operation_id} 未能启动（{type(exc).__name__}）；原输入保留。")

    @Slot(object)
    def _on_image_event(self, event: ConversationEvent) -> None:
        if self._image_worker is None or event.operation_id != self._image_worker.operation_id:
            return
        if event.kind == "completed":
            for info in event.data:
                self._images[info.attachment_id] = info
            self._image_label.setText("\n".join(
                f"{info.name} · {info.width} × {info.height} · {info.byte_size // 1024} KiB"
                for info in self._images.values()
            ))
            self._status_label.setText("图片已保存；是否允许发送由所选模型的图片能力决定。")
        elif event.kind == "failed":
            self._status_label.setText(f"图片未加入本次输入：{event.error}")

    @Slot()
    def _on_image_finished(self) -> None:
        worker = self._image_worker
        self._image_worker = None
        worker.deleteLater()
        self._image_button.setEnabled(True)
        self._clear_images_button.setEnabled(True)
        self._run_button.setEnabled(self._worker is None)

    @Slot()
    def _clear_images(self) -> None:
        """只清空输入引用；已保存附件和历史记录保留。"""
        self._images.clear()
        self._image_label.setText("尚未选择图片；GIF／动画 WebP 只发送第一帧，原文件保留。")

    def _read_payload(self) -> dict[str, Any]:
        """点击时取出输入快照；后台只使用这些值，不再访问控件。"""
        prompt = self._prompt_input.toPlainText()
        references = [info.as_content() for info in self._images.values()]
        user_message(prompt, references)
        temperature = parse_temperature(self._temperature_input.text())
        return {
            "prompt": prompt, "system": self._system_input.toPlainText(),
            "temperature": temperature,
            "max_output_tokens": parse_max_output_tokens(self._max_output_input.text()),
            "images": [asdict(part) for part in references],
            "reasoning_effort": self._effort_input.currentData(),
        }

    def showEvent(self, event: QShowEvent) -> None:
        """页面再次可见才刷新选择；不是后台监听或每次流式通知都读配置。"""
        super().showEvent(event)
        if self._worker is not None:
            return
        try:
            self._refresh_efforts(load_settings(self._settings_path))
        except (OSError, ValueError) as exc:
            self._status_label.setText(f"读取模型失败：{exc}；输入保留。")

    def _refresh_efforts(self, config: ModelConfig | None) -> bool:
        """只有模型快照变化才重建菜单，不把旧模型的档位转给新模型。"""
        if config == self._effort_config:
            return False
        self._effort_config = config
        self._effort_input.clear()
        for effort in available_efforts(config.reasoning if config else None):
            self._effort_input.addItem("关闭强度设置" if effort is None else "关闭思考" if effort == "none" else effort, effort)
        self._effort_input.setEnabled(self._effort_input.count() > 1)
        return True

    @Slot()
    def _on_run(self) -> None:
        """准备好本次配置和输入，再启动一次后台动作。"""
        if self._worker is not None or self._image_worker is not None:
            return
        try:
            config = load_settings(self._settings_path)
            if config is None:
                raise ValueError("请先在模型配置页保存配置。")
            chosen = self._effort_input.currentData()
            if self._refresh_efforts(config) and chosen is not None:
                raise ValueError("默认模型或能力已变化，请重新选择推理档位后运行。")
            api_key = resolve_api_key(config)
            payload = self._read_payload()
            runtime = create_runtime(config, api_key, attachments=self._attachments)
        except (OSError, ValueError) as exc:
            self._status_label.setText(f"无法开始：{exc}")
            return

        plugin_id = "model_trial"
        action = self._action_input.currentData()
        stream = self._stream_input.isChecked()
        worker = ActionWorker(runtime, plugin_id, action, payload, self, stream=stream)
        try:
            record = self._runs.begin_run(
                run_id=worker.run_id, plugin_id=plugin_id, action=action,
                config=config, payload=payload, stream=stream,
            )
        except (OSError, ValueError, TypeError) as exc:
            worker.deleteLater()
            self._status_label.setText(f"启动记录未能保存，本次未发送请求：{exc}")
            return
        self._history.show_started(record)
        self._worker = worker
        self._active_run_id = worker.run_id
        self._cancel_pending = False
        worker.progress.connect(self._on_run_event, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._on_thread_finished, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(worker.deleteLater)
        self._run_button.setEnabled(False)
        self._cancel_button.setEnabled(True)
        self._output.clear()
        self._details_label.clear()
        self._status_label.setText("正在启动")
        try:
            worker.start()
        except Exception as exc:
            # 已保存启动记录，补存失败终态；请求尚未执行，不能永久保留运行状态。
            self._on_run_event(RunEvent(
                worker.run_id, plugin_id, action, RunState.FAILED,
                error="后台线程未能启动，本次没有请求模型。", diagnostics=exception_diagnostics(exc),
            ))
            self._on_thread_finished()
            worker.deleteLater()

    @Slot()
    def _on_cancel(self) -> None:
        """请求取消后等待终态通知，保留已显示的片段。"""
        if self._worker is not None and self._worker.request_cancel():
            self._cancel_pending = True
            self._cancel_button.setEnabled(False)
            self._status_label.setText("正在取消，已显示内容尚未完成。")

    @Slot(object)
    def _on_run_event(self, event: RunEvent) -> None:
        """排队回到界面线程后更新控件，结果按运行编号对应。"""
        if event.run_id != self._active_run_id:
            return
        # 先交给运行管理对象保留事实，再按本页的显示状态更新控件。
        record, save_error = self._runs.accept_event(event)
        if event.state == RunState.RUNNING:
            if event.request is not None:
                self._history.show_requests(record)
            if self._cancel_pending:
                return
            self._status_label.setText("运行中")
            if event.attempt is not None:
                attempt = event.attempt
                self._output.moveCursor(QTextCursor.MoveOperation.End)
                if attempt.phase == "started":
                    prefix = "\n\n" if self._output.toPlainText() else ""
                    self._output.insertPlainText(f"{prefix}回复 {attempt.attempt}\n")
                    self._status_label.setText(f"正在进行第 {attempt.attempt} 次请求")
                elif attempt.phase in ("failed", "cancelled"):
                    detail = attempt.error or "已取消"
                    self._output.insertPlainText(f"\n[未完成：{detail}]\n")
                    self._status_label.setText("连接中断，正在重新请求" if attempt.retrying else detail)
            if event.text_delta is not None:
                self._output.moveCursor(QTextCursor.MoveOperation.End)
                self._output.insertPlainText(event.text_delta)
                self._output.ensureCursorVisible()
            return
        self._history.show_finished(record, save_error)
        self._cancel_button.setEnabled(False)
        if event.state == RunState.CANCELLED:
            self._output.setPlainText(format_replies(record.attempts, record.partial_text))
            self._status_label.setText("已取消；保留的文本是未完成输出。")
        elif event.state == RunState.FAILED:
            self._output.setPlainText(format_replies(record.attempts, record.partial_text))
            self._status_label.setText(f"运行失败：{event.error}")
            if self._output.toPlainText():
                self._details_label.setText("已显示文本尚未完成，不能作为完整结果。")
            if event.diagnostics:
                self._status_label.setText(self._status_label.text() + " 位置见历史与请求页的错误定位。")
        elif event.state == RunState.SUCCEEDED:
            response = event.result
            self._output.setPlainText(format_replies(record.attempts, response.text))
            self._status_label.setText("已完成")
            usage = json.dumps(asdict(response.token_usage) if response.token_usage else None, ensure_ascii=False)
            self._details_label.setText(f"结束原因：{response.finish_reason}；统一用量（null 表示未知）：{usage}；原始 usage 在历史记录中保留。")
            if response.completion_status == "incomplete":
                self._status_label.setText("已返回文本，但生成未完成，请查看结束原因。")
            elif response.completion_status == "unknown":
                self._status_label.setText("已返回文本，但服务未提供可确认的完成状态，请人工检查。")
            elif response.completion_status == "requires_tool":
                self._status_label.setText("模型请求工具结果；本页尚未执行工具。")
                calls = json.dumps([asdict(call) for call in response.tool_calls], ensure_ascii=False, indent=2)
                self._output.setPlainText(format_replies(record.attempts, response.text + "\n\n工具调用：\n" + calls))
        if save_error is not None:
            self._status_label.setText(
                self._status_label.text() + " 结果保存失败，请到历史与请求页查看状态或重试保存。"
            )

    @Slot()
    def _on_thread_finished(self) -> None:
        """线程真正退出后才允许再次运行，不在界面线程等待网络结束。"""
        self._worker = None
        self._cancel_pending = False
        self._run_button.setEnabled(self._image_worker is None)
        self._cancel_button.setEnabled(False)
