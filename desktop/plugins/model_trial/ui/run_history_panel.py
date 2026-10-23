"""运行历史与请求消息的只读展示；记录更新和保存交给 RunTracker。"""
import json
from dataclasses import asdict

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QComboBox, QLabel, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from ....llm.models import API_STYLES
from ....runtime.runs import RunState
from ....runtime.session import RunRecord, RunTracker
from .attempt_view import format_replies


STATE_LABELS = {
    RunState.RUNNING: "尚未记录结束",
    RunState.SUCCEEDED: "调用完成",
    RunState.FAILED: "失败",
    RunState.CANCELLED: "取消",
}


class RunHistoryPanel(QWidget):
    """只显示记录，不把历史自动加入下一次输入，也不恢复执行。"""

    def __init__(self, tracker: RunTracker) -> None:
        super().__init__()
        self._runs = tracker
        self._running_run_id: str | None = None
        self._build_form()
        self._load_history()

    def _build_form(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("查看历史不会发送请求；输入内容和模型结果保存在本地。"))
        self._history_input = QComboBox()
        self._history_input.setPlaceholderText("尚无运行记录")
        self._summary_label = QLabel()
        self._summary_label.setWordWrap(True)
        self._summary_label.setTextFormat(Qt.TextFormat.PlainText)
        self._request_view = QPlainTextEdit()
        self._request_view.setReadOnly(True)
        self._request_view.setPlaceholderText("这里显示插件交给 Provider 的请求消息，不代表服务器已收到。")
        self._result_view = QPlainTextEdit()
        self._result_view.setReadOnly(True)
        self._input_view = QPlainTextEdit()
        self._input_view.setReadOnly(True)
        self._diagnostic_view = QPlainTextEdit()
        self._diagnostic_view.setReadOnly(True)
        self._diagnostic_view.setPlaceholderText("意外错误发生时显示类型和调用位置，不记录请求正文或局部变量。")
        views = QTabWidget()
        views.addTab(self._request_view, "请求消息")
        views.addTab(self._result_view, "结果与状态")
        views.addTab(self._input_view, "当时的输入")
        views.addTab(self._diagnostic_view, "错误定位")
        self._storage_label = QLabel()
        self._storage_label.setWordWrap(True)
        self._storage_label.setTextFormat(Qt.TextFormat.PlainText)
        self._load_error_label = QLabel()
        self._load_error_label.setWordWrap(True)
        self._load_error_label.setTextFormat(Qt.TextFormat.PlainText)
        self._retry_button = QPushButton("重试保存待存结果")
        self._retry_button.setEnabled(False)
        for widget in (
            self._history_input, self._summary_label, views, self._storage_label,
            self._retry_button, self._load_error_label,
        ):
            layout.addWidget(widget)
        self._history_input.currentIndexChanged.connect(self._on_select)
        self._retry_button.clicked.connect(self._retry_save)

    def _load_history(self) -> None:
        """窗口创建时恢复显示，不创建模型后端；坏文件原样保留。"""
        try:
            records, errors = self._runs.load_history()
        except OSError as exc:
            self._load_error_label.setText(f"无法读取历史：{exc}")
            return
        for record in records:
            self._history_input.addItem(self._record_title(record), record.run_id)
        if errors:
            self._load_error_label.setText("以下记录未能读取，文件已保留：\n" + "\n".join(errors))

    def show_started(self, record: RunRecord) -> None:
        """显示已经保存的启动记录，不在面板里创建或更新记录。"""
        self._running_run_id = record.run_id
        self._history_input.insertItem(0, self._record_title(record), record.run_id)
        self._history_input.setCurrentIndex(0)
        self._history_input.setEnabled(False)
        self._retry_button.setEnabled(False)
        self._show_record(record)
        self._storage_label.setText("启动记录已保存；请求和结果将在运行结束时保存。" + self._pending_note())

    def show_requests(self, record: RunRecord) -> None:
        """只更新本次请求展示；不会将历史拼进模型输入。"""
        self._request_view.setPlainText(json.dumps(
            [asdict(request) for request in record.requests], ensure_ascii=False, indent=2,
        ))

    def show_finished(self, record: RunRecord, save_error: str | None) -> None:
        """根据运行管理对象的实际结果，分别展示运行状态和保存状态。"""
        self._running_run_id = None
        index = self._history_input.findData(record.run_id)
        self._history_input.setItemText(index, self._record_title(record))
        self._history_input.setEnabled(True)
        self._show_record(record)
        if save_error is not None:
            self._storage_label.setText(f"结果尚未保存：{save_error}。结果保留在窗口内，请重试保存。" + self._pending_note())
        else:
            self._storage_label.setText("本次请求与结果已保存。" + self._pending_note())
        self._retry_button.setEnabled(self.has_pending_records())

    def has_pending_records(self) -> bool:
        """供窗口关闭处理检查真实的待存状态，不读取或修改文件。"""
        return self._runs.pending_count > 0

    @Slot()
    def _retry_save(self) -> None:
        """重试写文件，不调用插件或模型。"""
        errors = self._runs.retry_pending()
        self._retry_button.setEnabled(self.has_pending_records())
        if errors:
            self._storage_label.setText("仍有结果未保存：\n" + "\n".join(errors))
        else:
            self._storage_label.setText("待存结果均已保存；没有重新调用模型。")

    @Slot(int)
    def _on_select(self, index: int) -> None:
        if index >= 0:
            run_id = self._history_input.itemData(index)
            self._show_record(self._runs.get_record(run_id))

    def _show_record(self, record: RunRecord) -> None:
        """历史请求和当时的表单输入分开展示，概括动作的改写也能看见。"""
        mode = "流式" if record.stream else "非流式"
        protocol = API_STYLES.get(record.config.api_style, record.config.api_style)
        self._summary_label.setText(
            f"模型：{record.config.model}；协议：{protocol}；方式：{mode}；编号：{record.run_id}\n"
            f"接口：{record.config.endpoint_url}；连接等待：{record.config.connect_timeout_seconds} 秒；"
            f"读取等待：{record.config.timeout_seconds} 秒；最多 {record.config.max_attempts} 次尝试\n"
            f"密钥环境变量名：{record.config.key_env}（不记录密钥值）\n"
            f"开始：{record.started_at}；结束：{record.finished_at or '未记录'}"
        )
        if record.requests:
            self.show_requests(record)
        else:
            self._request_view.clear()
        self._input_view.setPlainText(json.dumps(record.payload, ensure_ascii=False, indent=2))
        self._result_view.setPlainText(self._result_text(record))
        self._diagnostic_view.setPlainText(record.diagnostics or "")

    def _result_text(self, record: RunRecord) -> str:
        status = STATE_LABELS[record.state]
        if record.state == RunState.RUNNING:
            status = "运行中" if record.run_id == self._running_run_id else "未记录终态；可能仍在运行或进程已中断，不会自动重发。"
        if record.result is not None:
            # 当前试运行页保存 ModelResponse 的 JSON 字段；其他业务页面决定自己的结果展示。
            text = record.result.get("text")
            if not isinstance(text, str):
                text = json.dumps(record.result, ensure_ascii=False, indent=2)
            reason = record.result.get("finish_reason")
            usage = json.dumps(record.result.get("usage"), ensure_ascii=False)
            # 旧记录没有统一完成状态，继续识别此前的 length 标记。
            completion = record.result.get("completion_status", "incomplete" if reason == "length" else "completed")
            if completion == "incomplete":
                status += "；生成未完成，请查看结束原因"
            elif completion == "unknown":
                status += "；服务未提供可确认的完成状态"
            elif completion == "requires_tool":
                status += "；模型请求工具结果，本阶段未执行工具"
                calls = json.dumps(record.result.get("tool_calls", []), ensure_ascii=False, indent=2)
                text += "\n\n工具调用：\n" + calls
            return f"{status}\n结束原因：{reason}；用量仅含返回统计：{usage}\n\n{format_replies(record.attempts, text)}"
        if record.partial_text:
            status += "\n以下文本是未完成片段，不能作为完整结果。"
        if record.error:
            status += f"\n错误：{record.error}"
        return f"{status}\n\n{format_replies(record.attempts, record.partial_text)}"

    def _record_title(self, record: RunRecord) -> str:
        return f"{record.started_at} · {record.action} · {STATE_LABELS[record.state]}"

    def _pending_note(self) -> str:
        count = self._runs.pending_count
        return f"另有 {count} 条结果待保存。" if count else ""
