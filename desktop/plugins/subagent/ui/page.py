"""子任务目录和详情共用主聊天渲染；视图关闭不会停止执行器。"""
from dataclasses import replace
import asyncio
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QStackedWidget, QVBoxLayout, QWidget

from ....attachments.store import ImageStore
from ....ui.chat.projection import DisplayRow, history_rows
from ....ui.chat.transcript import Transcript
from ....ui.chat.context import ContextMeter
from ....ui.chat.usage_sample import from_steps, usage_after_step
from ....runtime.context.budget import effective_window
from ....ui.conversations.worker import ConversationWorker
from ....ui.contracts import CloseBlock
from ....ui.common.buttons import MotionButton
from ....ui.common.scrolling import SmoothScrollArea
from ....ui.common.surfaces import WhiteSurface
from ....ui.common.widgets import IconButton, ElidedLabel
from ....ui.shell import theme

LABELS = {"starting": "正在启动", "running": "正在工作", "cancelling": "正在停止", "completed": "已完成",
          "failed": "失败", "timed_out": "超时", "interrupted": "已停止", "save_failed": "保存未确认"}


def _readonly(row):
    process = replace(row.process, rows=tuple(_readonly(item) for item in row.process.rows)) if row.process else None
    return replace(row, anchor=None, editable=False, process=process, label="主 Agent" if row.label == "你" else row.label)


class SubagentPage(WhiteSurface):
    def __init__(self, access, directory, context):
        super().__init__()
        self.access, self.context = access, context
        self.child_id = context.resource_id
        self._stamp = None
        self._list_stamp = None
        self._live_version = None
        self._rows = ()
        self._save_worker = None
        self._row_records = {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
        self.catalog = SmoothScrollArea()
        self.catalog.setFrameShape(SmoothScrollArea.Shape.NoFrame)
        self.catalog.setWidgetResizable(True)
        content = WhiteSurface()
        self._cards = QVBoxLayout(content)
        self._cards.setContentsMargins(20, 24, 20, 20)
        self._cards.setSpacing(12)
        self.catalog.setWidget(content)
        self.stack.addWidget(self.catalog)
        detail = WhiteSurface()
        box = QVBoxLayout(detail)
        box.setContentsMargins(12, 8, 12, 8)
        header = QHBoxLayout()
        back = IconButton("undo", "返回子任务列表")
        back.clicked.connect(self._back)
        self.name = ElidedLabel()
        self.model = QLabel()
        self.model.setObjectName("settingsNote")
        self.stop = MotionButton("停止", variant="secondary")
        self.stop.clicked.connect(self._stop)
        header.addWidget(back)
        header.addWidget(self.name, 1)
        header.addWidget(self.model)
        self.context_meter = ContextMeter()
        self.context_meter.hide()
        header.addWidget(self.context_meter)
        header.addWidget(self.stop)
        box.addLayout(header)
        self.elapsed = QLabel()
        self.elapsed.setObjectName("settingsNote")
        box.addWidget(self.elapsed)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet("background: #e9eaec; border: none;")
        box.addWidget(line)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setObjectName("settingsNote")
        box.addWidget(self.status)
        self.retry = MotionButton("重试保存", variant="secondary")
        self.retry.clicked.connect(self._retry_save)
        self.retry.hide()
        box.addWidget(self.retry, 0, Qt.AlignmentFlag.AlignLeft)
        self.transcript = Transcript(ImageStore(directory / "attachments"))
        box.addWidget(self.transcript, 1)
        self.stack.addWidget(detail)
        self._timer = QTimer(self)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._refresh)
        self.bind_context(context)

    def close_block(self):
        return CloseBlock("保存尚未结束", "请等待原子结果保存完成；不会重做模型或工具。") if self._save_worker else None

    def _retry_save(self):
        if self._save_worker is not None:
            return
        parent_id, child_id = self.context.conversation_id, self.child_id
        async def save(_):
            await asyncio.to_thread(self.access.retry_save, parent_id, child_id)
        worker = ConversationWorker(save, self)
        self._save_worker = worker
        worker.progress.connect(lambda event: self.status.setText(event.error or "保存已确认") if event.kind in {"failed", "completed"} else None)
        def finished():
            self._save_worker = None
            worker.deleteLater()
            self._stamp = None
            self._refresh()
        worker.finished.connect(finished)
        worker.start()

    def bind_context(self, context):
        changed = self.context.conversation_id != context.conversation_id
        self.context = context
        if changed:
            self.child_id = None
            self._list_stamp = None
        if context.resource_id:
            self.child_id = context.resource_id
            self._stamp = None
        self.stack.setCurrentIndex(1 if self.child_id else 0)
        self._refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self._timer.start()
        self._refresh()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def _back(self):
        self.child_id = None
        self.stack.setCurrentIndex(0)
        self._list_stamp = None
        self._refresh()

    def _open(self, child_id):
        self.child_id = child_id
        self._stamp = self._live_version = None
        self.transcript.bind_id(child_id)
        self.transcript.set_history(())
        self.transcript.set_live(())
        self.stack.setCurrentIndex(1)
        self._refresh()

    def _stop(self):
        if self.child_id:
            try:
                self.access.cancel(self.context.conversation_id, self.child_id)
            except (OSError, ValueError) as exc:
                self.status.setText(str(exc))

    @staticmethod
    def duration(record):
        try:
            start = datetime.fromisoformat(record["started_at"])
            end = datetime.fromisoformat(record["finished_at"]) if record.get("finished_at") else datetime.now(timezone.utc)
            seconds = max(0, int((end - start).total_seconds()))
            return f"用时 {seconds // 60}分 {seconds % 60}秒"
        except (ValueError, TypeError):
            return "用时未知"

    @classmethod
    def list_time(cls, record):
        if not record.get("finished_at"):
            return cls.duration(record)
        try:
            seconds = max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(record["finished_at"])).total_seconds()))
            if seconds < 60:
                return "刚刚"
            if seconds < 3600:
                return f"{seconds // 60}分钟前"
            if seconds < 86400:
                return f"{seconds // 3600}小时前"
            return f"{seconds // 86400}天前"
        except (ValueError, TypeError):
            return "时间未知"

    def _refresh(self):
        if not self.context.conversation_id:
            return
        errors = self.access.errors()
        if errors:
            self.status.setText("\n".join(errors))
        records = self.access.list(self.context.conversation_id)
        if self.child_id is None:
            stamp = tuple((item["child_id"], item["status"], item["run_id"]) for item in records)
            if stamp != self._list_stamp:
                self._list_stamp = stamp
                while self._cards.count():
                    item = self._cards.takeAt(0)
                    if item.widget():
                        item.widget().deleteLater()
                self._row_records = {}
                groups = (("进行中", {"starting", "running", "cancelling"}), ("已完成", {"completed"}),
                          ("已停止或失败", {"failed", "timed_out", "interrupted", "save_failed"}))
                for label, states in groups:
                    values = [item for item in records if item["status"] in states]
                    heading = QLabel(f"{label} · {len(values)}")
                    heading.setObjectName("settingsNote")
                    self._cards.addWidget(heading)
                    for record in values:
                        row = QWidget()
                        layout = QHBoxLayout(row)
                        layout.setContentsMargins(4, 4, 4, 4)
                        button = MotionButton(record["name"], variant="secondary")
                        button.setStyleSheet("QPushButton { background: transparent; border: none; text-align: left; padding: 8px; border-radius: 9px; } QPushButton:hover { background: #f0f0f0; }")
                        button.clicked.connect(lambda _checked=False, key=record["child_id"]: self._open(key))
                        elapsed = QLabel(self.list_time(record))
                        elapsed.setObjectName("settingsNote")
                        layout.addWidget(button, 1)
                        layout.addWidget(elapsed)
                        self._row_records[record["child_id"]] = elapsed
                        self._cards.addWidget(row)
                if not records:
                    empty = QLabel("尚未启动子任务")
                    empty.setObjectName("settingsNote")
                    self._cards.addWidget(empty)
                self._cards.addStretch()
            for item in records:
                label = self._row_records.get(item["child_id"])
                if label:
                    label.setText(self.list_time(item))
            return
        record = next((item for item in records if item["child_id"] == self.child_id), None)
        if record is None:
            self.status.setText("此子任务不属于当前会话。")
            return
        self.name.setText(record["name"])
        self.model.setText(record["model"] + (" · " + record["effort"] if record.get("effort") else ""))
        self.elapsed.setText(self.duration(record))
        active = record["status"] in {"starting", "running", "cancelling"}
        self.stop.setVisible(active)
        self.stop.setEnabled(record["status"] != "cancelling")
        self.status.setText(record.get("error") or LABELS.get(record["status"], record["status"]))
        self.retry.setVisible(record["status"] == "save_failed")
        self.retry.setEnabled(self._save_worker is None)
        try:
            stamp = self.access.stamp(self.context.conversation_id, self.child_id)
            if stamp != self._stamp:
                conversation = self.access.conversation(self.context.conversation_id, self.child_id)
                rows, markers = history_rows(conversation, include_pending=False)
                if active and conversation.active_turn:
                    run_id = conversation.active_turn.run_id
                    rows = tuple(replace(row, process=replace(row.process, running=True)) if row.process and row.key.startswith("work:" + run_id)
                                 else row for row in rows if row.key != "notice:" + run_id)
                self._rows = tuple(_readonly(row) for row in rows)
                self.transcript.set_history(self._rows)
                self.transcript.set_markers(markers)
                self._stamp = stamp
                turn = conversation.turns[-1] if conversation.turns else None
                sample = from_steps(turn.steps if turn else [], running=active,
                    window=effective_window(conversation.settings.config.context_window, conversation.settings.context_policy.context_window),
                    after_step=usage_after_step(turn.run_id, turn.started_at, conversation.latest_compaction) if turn else None)
                latest = sample.latest
                usage = latest.usage if latest else None
                self.context_meter.set_usage(usage.input_tokens if usage else None, usage.output_tokens if usage else None,
                    latest.window if latest else None, cache_read=sample.cache_read, cache_input=sample.cache_input,
                    request_seconds=latest.seconds if latest else None, settled_requests=sample.settled_requests, running=active)
            live = self.access.live(self.context.conversation_id, self.child_id)
            if active and live.get("version") != self._live_version:
                rows = []
                if live.get("reasoning_active"):
                    for block in live.get("blocks", ()):
                        rows.append(DisplayRow("child-reasoning:" + block["block_id"], "", block["text"],
                                               reasoning=True, reasoning_running=True, reasoning_kind=block["kind"]))
                if live.get("text"):
                    rows.append(DisplayRow("child-live", "模型", live["text"], markdown=False))
                self.transcript.set_live(tuple(rows))
                self._live_version = live.get("version")
            elif not active:
                self.transcript.set_live(())
        except (OSError, ValueError) as exc:
            if record["status"] != "starting":
                self.status.setText(f"子日志读取失败，已有内容保留：{exc}")
