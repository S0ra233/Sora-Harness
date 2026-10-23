"""绑定一个固定控制器的聊天视图；自身只保管显示、搜索和绘制节奏。"""
from PySide6.QtCore import QEvent, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from ..common.buttons import MotionButton
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ...application.conversations import ConversationApplication
from ...runtime.agent.models import AgentResult
from ...runtime.conversations import Conversation
from ...runtime.context.budget import effective_window
from ..conversations import ConversationController, ConversationState
from ..conversations.worker import ConversationEvent
from .composer import Composer
from .questions import QuestionDeck
from .navigation import TurnNavigation
from .projection import TurnMarker, history_rows, input_row, live_rows, restrict_revision_rows
from .usage_sample import UsageSample, from_steps, from_live, usage_after_step, request_after_compaction
from .search import TranscriptSearch
from .transcript import Transcript
from .tail_button import TailButton
from ..common.widgets import ElidedLabel, IconGlyph
from ..shell import theme
from ..common.dialogs import HistoryConfirmation
from ..contracts import UiPageContext
from ...runtime.conversations.revisions import MessageAnchor


class ChatPanel(QWidget):
    branch_requested = Signal(object)
    page_requested = Signal(str, str)
    def __init__(self, application: ConversationApplication, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("chatPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._application = application
        self.row_decorator = None
        self._controller: ConversationController | None = None
        self._history_markers = ()
        self._history_keys = set()
        self._navigation_source = None
        self._action_error: str | None = None
        self._read_only_reason: str | None = None
        self._navigation_id = None
        self._history_usage = UsageSample()
        self._last_request_usage = None
        self._usage_dirty = True
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._render_live)
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self._schedule_live)
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.transcript = Transcript(self._application.attachments)
        self.transcript.action_requested.connect(self._message_action)
        self.transcript.page_requested.connect(self.page_requested.emit)
        self.search = TranscriptSearch(self.transcript)
        self._navigation = TurnNavigation()
        self._stage = QWidget()
        self.search.setParent(self._stage)
        body = QVBoxLayout(self._stage)
        body.setContentsMargins(0, 0, 0, 0)
        body.addWidget(self.transcript)
        self._navigation.setParent(self._stage)
        self._navigation.raise_()
        self._tail = TailButton(self)
        self._tail.clicked.connect(self.transcript.scroll_to_bottom)
        self.transcript.away_from_bottom_changed.connect(self._tail.set_away)
        self._stage.installEventFilter(self)
        self._dock = QWidget()
        self._dock.setObjectName("composerDock")
        self._dock.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._dock_layout = QVBoxLayout(self._dock)
        self._dock_layout.setContentsMargins(0, 12, 6, 21)
        self._dock_layout.setSpacing(8)
        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.addStretch(1)
        column = QWidget()
        self._input_column = column
        column.setMaximumWidth(theme.CONTENT_WIDTH)
        column_layout = QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(8)
        self._scope_row = QWidget()
        scope_layout = QHBoxLayout(self._scope_row)
        scope_layout.setContentsMargins(9, 0, 9, 0)
        scope_layout.addWidget(IconGlyph("folder", size=14))
        self._scope_label = ElidedLabel()
        self._scope_label.setStyleSheet(f"color: {theme.FAINT}; font-size: 11px;")
        scope_layout.addWidget(self._scope_label, 1)
        self._scope_row.hide()
        self.composer = Composer(self._application.attachments)
        self.composer.installEventFilter(self)
        self.questions = QuestionDeck()
        self.questions.answered.connect(self._answer_question)
        column_layout.addWidget(self.questions)
        column_layout.addWidget(self._scope_row)
        column_layout.addWidget(self.composer)
        input_row.addWidget(column, 100)
        input_row.addStretch(1)
        self._status_row = QFrame()
        self._status_row.setObjectName("chatAlert")
        status_row = QHBoxLayout(self._status_row)
        status_row.setContentsMargins(12, 8, 12, 8)
        self._status = QLabel()
        self._status.setObjectName("chatStatus")
        self._status.setTextFormat(Qt.TextFormat.PlainText)
        self._status.setWordWrap(True)
        self._retry = MotionButton("重试保存", variant="secondary")
        self._retry.setObjectName("retryButton")
        self._retry.hide()
        status_row.addWidget(self._status, 1)
        status_row.addWidget(self._retry)
        column_layout.addWidget(self._status_row)
        self._status_row.hide()
        self._dock_layout.addLayout(input_row)
        layout.addWidget(self._stage, 1)
        layout.addWidget(self._dock)
        self._navigation.activated.connect(self.transcript.jump)
        self.transcript.active_turn_changed.connect(self._navigation.set_active)
        self.composer.draft_changed.connect(self._draft)
        self.composer.send_requested.connect(self._send)
        self.composer.queue_requested.connect(self._queue)
        self.composer.stop_requested.connect(self._stop)
        self.composer.images_requested.connect(self._attach)
        self.composer.image_removed.connect(self._remove_image)
        self.composer.model_selection_changed.connect(self._model_changed)
        self.transcript.reading_width_changed.connect(self._input_column.setFixedWidth)
        self._input_column.setFixedWidth(self.transcript.reading_width)
        self._retry.clicked.connect(self._retry_save)
        shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        shortcut.activated.connect(self.search.open_search)

    def set_scope(self, workspace: str | None, project_name: str | None = None) -> None:
        """页面提供展示资料；不会通过这行文字改变 ToolContext 或工作目录。"""
        self._scope_row.setVisible(workspace is not None)
        self._scope_label.setText((project_name or "项目") if workspace else "")
        self._scope_label.setToolTip(workspace or "")
        self.transcript.set_welcome(project_name)

    def _message_action(self, action: str, anchor: MessageAnchor, text) -> None:
        controller = self._controller
        if controller is None or anchor is None or self._read_only_reason:
            return
        try:
            reason = None if action == "fork" else controller.revision_unavailable()
        except ValueError as exc:
            reason = str(exc)
        if reason:
            self._action_error = reason
            self._restore_state()
            return
        dialog = HistoryConfirmation(action, self, has_draft=bool(controller.state.pending_input or controller.state.pending_images),
                                     running=controller.run_is_active)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        dialog.deleteLater()
        if not accepted:
            return
        try:
            if action == "fork":
                self.branch_requested.emit(anchor)
            elif action == "regenerate":
                controller.regenerate(anchor)
            else:
                controller.revise(anchor, replacement=text if action == "edit" else None)
            self._action_error = None
        except (OSError, ValueError) as exc:
            self._action_error = str(exc)
        self._restore_state()

    def eventFilter(self, watched, event) -> bool:
        if watched is self._stage and event.type() == QEvent.Type.Resize:
            # 导航叠在正文留白内，不另占一列挤歪居中的消息和输入框。
            self._navigation.setGeometry(16, 20, 32, max(0, self._stage.height() - 40))
            self.search.place()
        if watched in {self._stage, self.composer} and event.type() in {QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.Show}:
            self._place_tail()
        return super().eventFilter(watched, event)

    def _place_tail(self) -> None:
        # 以真实输入台为锚点，项目标签或告警出现时仍保持居中和12px间距。
        anchor = self.composer.mapTo(self, self.composer.rect().topLeft())
        center = anchor.x() + self.composer.width() // 2
        y = anchor.y() - self._tail.height() - 12
        self._tail.move(center - self._tail.width() // 2, max(0, y))
        self._tail.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._input_column.setFixedWidth(self.transcript.reading_width)

    def bind(self, controller: ConversationController) -> None:
        self.unbind()
        self._controller = controller
        self._navigation_id = (controller.conversation.navigation or {}).get("operation_id") if controller.conversation else None
        self._history_usage = UsageSample()
        self._last_request_usage = None
        self._usage_dirty = True
        self._action_error = None
        controller.state_changed.connect(self._on_state)
        controller.conversation_changed.connect(self._on_conversation)
        controller.event_received.connect(self._on_event)
        controller.questions_changed.connect(self._show_questions)
        self._show_questions()
        self.transcript.bind_id(controller.state.conversation_id)
        if controller.conversation is not None:
            self._show_history(controller.conversation)
        else:
            self._history_markers = ()
            self._history_keys = set()
            self.transcript.set_history(())
            self.composer.set_settings(None)
            self.composer.context.set_usage(None, None, None)
        self._restore_state()
        self._render_live()

    def unbind(self) -> None:
        self._timer.stop()
        self._clock_timer.stop()
        self.search.close_search()
        if self._controller is not None:
            self._controller.state_changed.disconnect(self._on_state)
            self._controller.conversation_changed.disconnect(self._on_conversation)
            self._controller.event_received.disconnect(self._on_event)
            self._controller.questions_changed.disconnect(self._show_questions)
        self._controller = None
        self.questions.set_questions(None, ())
        self._tail.set_working(False)

    def _show_questions(self) -> None:
        if self._controller is not None:
            self.questions.set_questions(self._controller.state.conversation_id, self._controller.questions)
            self._place_tail()

    def _answer_question(self, key, answer) -> None:
        if self._controller is None or self._read_only_reason:
            return
        try:
            self._controller.answer_question(key, answer)
            self._action_error = None
        except (OSError, ValueError) as exc:
            self._action_error = str(exc)
        self._restore_state()
        self._tail.set_away(False)

    def clear(self) -> None:
        """只清除页面绑定和显示；原会话草稿、附件及日志仍保留。"""
        self.unbind()
        self._read_only_reason = None
        self._history_markers = ()
        self._history_keys = set()
        self._navigation_source = None
        self.transcript.bind_id(None)
        self.transcript.set_history(())
        self.transcript.set_live(())
        self.transcript.set_markers(())
        self._navigation.set_markers(())
        self.composer.setEnabled(True)
        self.composer.set_settings(None)
        self.composer.restore("", ())
        self.composer.set_state(False, False, False)
        self.composer.context.set_usage(None, None, None)
        self._status_row.hide()
        self.set_scope(None)

    def set_read_only(self, reason: str | None) -> None:
        self._read_only_reason = reason
        self._restore_state()

    @Slot(object)
    def _on_state(self, state: ConversationState) -> None:
        if self.sender() is self._controller:
            self._restore_state()
            self._schedule_live()

    @Slot(object)
    def _on_conversation(self, conversation: Conversation) -> None:
        if self.sender() is self._controller:
            navigation_id = (conversation.navigation or {}).get("operation_id")
            if navigation_id != self._navigation_id:
                self.transcript.finish_edits()
                self._navigation_id = navigation_id
            self.transcript.bind_id(conversation.conversation_id)
            self._show_history(conversation)
            self._schedule_live()

    @Slot(object)
    def _on_event(self, event: ConversationEvent) -> None:
        if self.sender() is not self._controller or event.operation_id != self._controller.state.operation_id:
            return
        if event.kind in {"completed", "failed", "cancelled"}:
            self._usage_dirty = True
            self._timer.stop()
            self._render_live()
        elif event.kind == "agent":
            if event.data.kind == "compaction_finished" and event.data.compaction is not None and event.data.compaction.status == "succeeded":
                self.transcript.finish_edits()
                if self._controller.conversation is not None:
                    self._show_history(self._controller.conversation)
            if event.data.kind in {"model_started", "model_attempt", "model_finished", "compaction_finished"}:
                self._usage_dirty = True
            self._schedule_live()

    def _show_history(self, conversation: Conversation) -> None:
        self._navigation_source = None
        rows, self._history_markers = history_rows(conversation, include_pending=False,
                                                 boundary=self._controller.revision_boundary)
        rows = self._decorate_rows(rows)
        self._history_keys = {row.key for row in rows}
        self.transcript.set_history(rows)
        settings = conversation.settings
        self.composer.set_settings(settings, self._controller.state.turn_options)
        navigation_time = max((conversation.navigation or {}).get("time") or "", conversation.prompt_applied_at or "")
        if navigation_time and (not conversation.turns or navigation_time >= (conversation.turns[-1].finished_at or conversation.turns[-1].started_at)):
            # 修订后尚未发过新请求，旧用量不能描述这份新的有效上下文。
            self._history_usage, self._last_request_usage = UsageSample(), None
            self._usage_dirty = True
            return
        boundary = self._controller.revision_boundary
        self._history_usage = self._turn_usage(conversation, conversation.turns[-1], boundary) if conversation.turns else UsageSample()
        self._last_request_usage = self._history_usage.latest
        if self._last_request_usage is None:
            # 末轮失败不抹掉上次真实窗口样本，但缓存不能借用旧轮的数据冒充本轮。
            for turn in reversed(conversation.turns[:-1]):
                sample = self._turn_usage(conversation, turn, boundary)
                if sample.latest is not None:
                    self._last_request_usage = sample.latest
                    break
        self._usage_dirty = True

    @staticmethod
    def _turn_usage(conversation, turn, boundary=None) -> UsageSample:
        config = turn.options.model_target.config if turn.options is not None and turn.options.model_target is not None else conversation.settings.config
        return from_steps(turn.steps, window=effective_window(config.context_window, conversation.settings.context_policy.context_window),
                          after_step=usage_after_step(turn.run_id, turn.started_at, boundary or conversation.latest_compaction))

    def set_model_catalog(self, catalog) -> None:
        self.composer.set_model_catalog(catalog)

    def _show_usage(self, view, result, running) -> None:
        """事件结算才更新统计；文字片段和滚轮帧不重新扫描历史。"""
        if view.conversation is None:
            self.composer.context.set_usage(None, None, None)
            return
        live = view.live if view.live is not None and view.live.operation_id == view.state.operation_id else None
        conversation = view.conversation
        compressed = self._controller.revision_boundary
        if compressed is not None and not request_after_compaction(conversation, compressed, live=live):
            # 摘要请求用量不等于新上下文；压缩成功后等待真正的新主请求。
            self._last_request_usage = None
            self.composer.context.set_usage(None, None, None)
            return
        sample = self._history_usage
        if live is not None and live.kind in {"send", "resume", "program"}:
            target = view.state.run_target
            config = target.config if target is not None else view.conversation.settings.config
            window = effective_window(config.context_window, view.conversation.settings.context_policy.context_window)
            after = usage_after_step(live.operation_id, live.started_at, compressed)
            sample = from_steps(result.steps, running=running, window=window, after_step=after) if result is not None else from_live(
                live.attempts, running=running, window=window, after_step=after)
        latest = sample.latest or self._last_request_usage
        changed_at = max((conversation.navigation or {}).get("time") or "", conversation.prompt_applied_at or "")
        last_turn = conversation.turns[-1] if conversation.turns else None
        if changed_at and changed_at >= (last_turn.finished_at or last_turn.started_at if last_turn else ""):
            if live is None or (live.started_at or "") <= changed_at or sample.latest is None:
                latest, sample = None, UsageSample(running=running)
        self._last_request_usage = latest
        usage = latest.usage if latest else None
        window = latest.window if latest is not None else None
        self.composer.context.set_usage(usage.input_tokens if usage else None, usage.output_tokens if usage else None, window,
            cache_read=sample.cache_read, cache_input=sample.cache_input,
            reasoning=usage.reasoning_tokens if usage else None, request_seconds=latest.seconds if latest else None,
            settled_requests=sample.settled_requests, running=sample.running, cache_scope=sample.scope)

    def _restore_state(self) -> None:
        controller = self._controller
        if controller is None:
            return
        state = controller.state
        if controller.run_is_active and self.isVisible():
            if not self._clock_timer.isActive():
                self._clock_timer.start()
        else:
            self._clock_timer.stop()
        self._tail.set_working(controller.run_is_active)
        self._tail.set_away(self.transcript.away_from_bottom)
        self.composer.restore(state.pending_input, state.pending_images)
        self.composer.set_state(controller.conversation is not None, controller.busy,
                                state.phase == "save_failed" or state.options_error is not None,
                                supplement=controller.can_supplement,
                                resumable=controller.can_resume or any(item.kind == "follow_up" for item in state.inputs))
        if controller.conversation is not None:
            options = state.turn_options
            self.composer.set_settings(controller.conversation.settings, options)
        self.composer.setEnabled(self._read_only_reason is None and not state.input_protected and not controller.revising)
        self.questions.setEnabled(self._read_only_reason is None and not state.input_protected and not controller.revising
                                  and state.phase != "save_failed")
        notice = self._read_only_reason or ("正在停止，请等待当前任务退出…" if state.phase == "cancelling" else "")
        error = state.error or state.draft_error or state.options_error or self._action_error
        self._status.setText(error or notice)
        if self._status.property("error") != bool(error):
            self._status.setProperty("error", bool(error))
            self._status.style().unpolish(self._status)
            self._status.style().polish(self._status)
            self._status_row.setProperty("error", bool(error))
            self._status_row.style().unpolish(self._status_row)
            self._status_row.style().polish(self._status_row)
        retry_needed = state.phase == "save_failed" or state.draft_error is not None or state.options_error is not None or controller.can_retry_revision
        self._retry.setVisible(retry_needed)
        self._retry.setEnabled(not controller.busy)
        self._status_row.setVisible(bool(error or notice) or retry_needed)

    def _schedule_live(self) -> None:
        if self.isVisible() and not self._timer.isActive():
            self._timer.start()

    def _render_live(self) -> None:
        if self._controller is None:
            return
        view = self._controller.snapshot()
        result = view.state.result if isinstance(view.state.result, AgentResult) else None
        running = self._controller.run_is_active
        if self._usage_dirty:
            self._show_usage(view, result, running)
            self._usage_dirty = False
        rows, markers = live_rows(view.live, result, running=running)
        live_keys = {row.key for row in rows}
        rows = (*rows, *(input_row(item) for item in view.state.inputs
                        if f"user:{item.input_id}" not in self._history_keys and f"user:{item.input_id}" not in live_keys))
        rows = self._decorate_rows(rows)
        self.transcript.set_live(rows)
        source = (*markers, *(TurnMarker(row.key, row.text[:240] or "图片消息") for row in rows if row.label == "你"))
        if source != self._navigation_source:
            # 导航摘要最多240字；后续正文和每秒计时变化不重建全部历史标记。
            unique = {marker.key: marker for marker in self._history_markers}
            for marker in source:
                unique.setdefault(marker.key, marker)
            values = tuple(unique.values())
            self.transcript.set_markers(values)
            self._navigation.set_markers(values)
            self._navigation_source = source
        self._restore_state()

    def _decorate_rows(self, rows):
        if self.row_decorator is not None:
            identifier = self._controller.state.conversation_id if self._controller is not None else None
            rows = self.row_decorator(rows, UiPageContext(conversation_id=identifier))
        boundary = self._controller.revision_boundary if self._controller is not None else None
        return restrict_revision_rows(rows, boundary)

    def refresh_decorations(self):
        self.transcript.refresh_rows(self._decorate_rows)

    def _perform(self, operation) -> None:
        self._action_error = None
        try:
            operation()
        except (OSError, ValueError) as exc:
            self._action_error = str(exc)
        self._restore_state()

    def _draft(self, text: str) -> None:
        if self._controller is not None and self._controller.conversation is not None:
            self._perform(lambda: self._controller.set_draft(text))

    def _send(self) -> None:
        if self._controller is not None and self._read_only_reason is None:
            if self.composer.page_commands is not None and self.composer.page_commands.exact():
                return
            def submit():
                was_running = self._controller.run_is_active
                self._controller.send_message(self.composer.editor.toPlainText())
                if not was_running:
                    self.transcript.resume_follow()
            self._perform(submit)

    def _queue(self) -> None:
        if self._controller is not None and self._read_only_reason is None:
            if self.composer.page_commands is not None and self.composer.page_commands.exact():
                return
            self._perform(lambda: self._controller.queue_message(self.composer.editor.toPlainText()))

    def _model_changed(self, selection) -> None:
        if self._controller is not None and self._read_only_reason is None:
            self._perform(lambda: self._controller.set_model_target(selection.target, selection.effort))

    def _stop(self) -> None:
        if self._controller is not None:
            operation_id = self._controller.state.operation_id
            if operation_id in self._controller.task_ids:
                self._controller.cancel(operation_id)
            else:
                # 模型已退出但图片导入尚在收尾时，停止入口仍指向真实在途操作。
                for identifier in self._controller.task_ids:
                    self._controller.cancel(identifier)

    def _attach(self, paths) -> None:
        if self._controller is not None and self._read_only_reason is None:
            self._perform(lambda: self._controller.attach_images(paths))

    def _remove_image(self, attachment_id: str) -> None:
        if self._controller is not None:
            images = [item for item in self._controller.state.pending_images if item.attachment_id != attachment_id]
            self._perform(lambda: self._controller.set_draft(self.composer.editor.toPlainText(), images))

    def compact_unavailable(self) -> str | None:
        if self._read_only_reason:
            return self._read_only_reason
        if self._controller is None:
            return "请先打开已有对话。"
        if self._controller.state.pending_images:
            return "/compact 不能附带图片；草稿图片已保留。"
        return self._controller.compact_unavailable()

    def start_compaction(self) -> str:
        reason = self.compact_unavailable()
        if reason:
            raise ValueError(reason)
        identifier = self._controller.compact()
        self._action_error = None
        self.transcript.resume_follow()
        return identifier

    def _retry_save(self) -> None:
        if self._controller is not None:
            if self._controller.state.phase == "save_failed":
                self._perform(self._controller.retry_save)
            elif self._controller.can_retry_revision:
                self._perform(self._controller.retry_revision)
            elif self._controller.state.input_protected:
                self._perform(lambda: self._controller.open_conversation(self._controller.conversation.conversation_id))
            else:
                self._perform(self._controller.flush_drafts)
                self._perform(self._controller.flush_turn_options)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._controller is not None:
            self._render_live()

    def hideEvent(self, event) -> None:
        self._clock_timer.stop()
        self._timer.stop()
        super().hideEvent(event)
