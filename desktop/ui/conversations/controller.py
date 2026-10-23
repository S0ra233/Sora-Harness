"""没有控件布局的会话控制器；界面订阅状态与事件，再决定怎样显示。"""
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID, uuid4

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ...application import ConversationApplication, ConversationUpdate
from ...application.tasks import TaskRegistry
from ...attachments.images import MAX_REQUEST_IMAGES
from ...llm.messages import ImageContent, user_message
from ...tools.questions import questions_from_result
from ...runtime.conversations.questions import AskedQuestion, collect_questions, reply_message
from ...llm.model_catalog import ModelTarget
from ...llm.diagnostics import RequestRecordNotice
from ...environment import EnvironmentCheck, EnvironmentSnapshot
from ...tools.catalog import DEFAULT_WORK_TOOLS
from ...runtime.agent.models import AgentEvent, AgentResult, CompactionStatus
from ...runtime.runs import exception_diagnostics
from ...runtime.conversations import CompactionRecord, CompactionSaveError, Conversation, ConversationSaveError, ConversationSettings, TurnOptions
from ...runtime.conversations.models import ConversationInput
from ...runtime.conversations.control import ConversationControl, InputNotice
from ...runtime.conversations.revisions import (
    COMPACTION_REVISION_ERROR, regeneration_anchor,
)
from ..contracts import CloseBlock
from .worker import ConversationEvent, ConversationWorker, Operation
from .live_view import LiveRunView, LiveSnapshot


DEFAULT_TOOLS = DEFAULT_WORK_TOOLS


def _selected_tools(names: Sequence[str]) -> tuple[str, ...]:
    """应用工具授权的输入边界；日志和页面绑定不能自动扩大授权。"""
    if isinstance(names, str):
        raise ValueError("工具名称须为字符串序列。")
    tools = tuple(names)
    if any(not isinstance(name, str) or not name for name in tools) or len(tools) != len(set(tools)):
        raise ValueError("工具名称须为非空且不重复的字符串。")
    return tools


@dataclass(frozen=True)
class ConversationState:
    phase: str = "idle"
    conversation_id: str | None = None
    pending_input: str = ""
    environment: EnvironmentCheck | None = None
    result: AgentResult | CompactionRecord | None = None
    error: str | None = None
    diagnostics: str | None = None
    draft_error: str | None = None
    pending_images: tuple[ImageContent, ...] = ()
    operation_id: str | None = None
    turn_options: TurnOptions | None = None
    options_error: str | None = None
    input_protected: bool = False
    record_next_turn: bool = False
    record_run_id: str | None = None
    record_warning: str | None = None
    inputs: tuple[ConversationInput, ...] = ()
    run_target: ModelTarget | None = None


@dataclass(frozen=True)
class ConversationView:
    """页面绑定时读取；conversation 是控制器持有的只读展示资料。"""

    state: ConversationState
    conversation: Conversation | None
    live: LiveSnapshot | None


@dataclass(frozen=True)
class _OperationState:
    kind: str
    tool_names: tuple[str, ...] | None
    draft_revision: int


class ConversationController(QObject):
    """固定一个会话的草稿与显示；主历史操作串行，其他控制器可以并发。"""

    state_changed = Signal(object)
    event_received = Signal(object)
    conversation_changed = Signal(object)
    environment_changed = Signal(object)
    task_finished = Signal(str)
    branch_created = Signal(object)
    questions_changed = Signal()

    def __init__(self, local_directory: Path, parent: QObject | None = None, *,
                 application: ConversationApplication | None = None, tasks: TaskRegistry | None = None,
                 conversation_id: str | None = None) -> None:
        super().__init__(parent)
        self.application = application if application is not None else ConversationApplication(local_directory)
        self.tasks = tasks if tasks is not None else TaskRegistry()
        self._conversation_id = UUID(conversation_id).hex if conversation_id is not None else None
        self.state = ConversationState(conversation_id=self._conversation_id)
        self.conversation: Conversation | None = None
        self._tools: tuple[str, ...] = ()
        self._operations: dict[str, _OperationState] = {}
        self._workers: dict[str, ConversationWorker] = {}
        self._submissions: dict[str, int] = {}
        self._queue_paused = True
        self._save_error: ConversationSaveError | CompactionSaveError | None = None
        self._draft_dirty = False
        self._draft_revision = 0
        self._draft_submission: str | None = None
        self._submitted: tuple[str, int] | None = None
        self._live_view: LiveRunView | None = None
        self._questions = {}
        self._question_submissions = {}
        self._pending_revision = None
        self._revision_retry = None
        self._after_revision = None
        self._fork_retry = None
        self._options_dirty = False
        self._options_timer = QTimer(self)
        self._options_timer.setSingleShot(True)
        self._options_timer.setInterval(400)
        self._options_timer.timeout.connect(self.flush_turn_options)
        self._draft_timer = QTimer(self)
        self._draft_timer.setSingleShot(True)
        self._draft_timer.setInterval(400)
        self._draft_timer.timeout.connect(self.flush_drafts)

    @property
    def busy(self) -> bool:
        # 终态通知早于 QThread.finished；仍须持有线程直到真正退出。
        return bool(self._operations)

    @property
    def task_ids(self) -> tuple[str, ...]:
        return tuple(self._operations)

    @property
    def has_pending_changes(self) -> bool:
        return (self._save_error is not None or self._draft_dirty or self._options_dirty or self.state.record_next_turn
                or self._revision_retry is not None or self._fork_retry is not None or self._after_revision is not None
                or bool(self.state.inputs) or self.can_resume
                or not self.state.input_protected and (self.state.draft_error is not None or self.state.options_error is not None))

    @property
    def has_live_run(self) -> bool:
        """界面判断是否已有本轮输入时，不必拼接全部流式文本。"""
        return self._live_view is not None

    @property
    def run_is_active(self) -> bool:
        """保留的旧失败片段不能因附件／保存任务变成新的模型运行。"""
        return (self._live_view is not None and self._live_view.operation_id == self.state.operation_id
                and self.busy and self.state.phase in {"running", "cancelling"})

    @property
    def can_resume(self) -> bool:
        return self.conversation is not None and self.conversation.resumable_turn is not None

    @property
    def revising(self) -> bool:
        return self._pending_revision is not None or any(item.kind == "revise" for item in self._operations.values())

    @property
    def can_retry_revision(self) -> bool:
        return self._revision_retry is not None

    @property
    def can_retry_fork(self) -> bool:
        return self._fork_retry is not None and not any(item.kind == "fork" for item in self._operations.values())

    @property
    def can_retry_regeneration(self) -> bool:
        return self._after_revision is not None and self._revision_retry is None

    @property
    def can_supplement(self) -> bool:
        worker = self._workers.get(self.state.operation_id)
        return (self.run_is_active and self.state.phase == "running" and worker is not None
                and worker.control is not None and self._save_error is None)

    def snapshot(self) -> ConversationView:
        """切回正在执行的会话只取内存，不争用 Core 的日志写锁。"""
        return ConversationView(self.state, self.conversation, self._live_view.snapshot() if self._live_view else None)

    def set_tool_names(self, tool_names: Sequence[str]) -> None:
        """工具授权由应用显式提供，运行中的快照不随页面绑定改变。"""
        tools = _selected_tools(tool_names)
        if self.busy and tools != self._tools:
            raise ValueError("任务仍在运行，请结束后再调整该会话的工具授权。")
        self._tools = tools

    def new_conversation(self, settings: ConversationSettings, *, required_programs: Sequence[str] = ()) -> str:
        if self._conversation_id is not None:
            raise ValueError("控制器已归属一个会话；请由 ConversationHub 新建另一个控制器。")
        settings, programs = deepcopy(settings), tuple(required_programs)

        async def create(_):
            return self.application.create(settings, required_programs=programs)

        return self._start("create", create, tool_names=tuple(settings.tool_names))

    def open_conversation(self, conversation_id: str, *, tool_names: Sequence[str] = DEFAULT_TOOLS) -> str:
        conversation_id = UUID(conversation_id).hex
        if self._conversation_id is not None and self._conversation_id != conversation_id:
            raise ValueError("控制器的会话归属固定；请由 ConversationHub 打开另一个会话。")
        tools = _selected_tools(tool_names)

        async def open_saved(_):
            return self.application.open(conversation_id)

        # 这是应用本次选择，不能由打开的日志自动扩大。
        if not self.busy:
            self._conversation_id = conversation_id
        return self._start("open", open_saved, tool_names=tools)

    def send_message(self, prompt: str, images: list[ImageContent] | None = None) -> str:
        if self.state.input_protected:
            raise ValueError("草稿或本轮选项读取失败，请先重新读取；历史仍可查看。")
        if self.busy:
            references = tuple(images) if images is not None else self.state.pending_images
            if (self._submitted is not None and self._submitted[1] == self._draft_revision
                    and prompt == self.state.pending_input and references == self.state.pending_images):
                return self._submitted[0]
            return self._submit_input(prompt, images, "steer")
        self._current_id()
        queue = self.conversation.pending_inputs("follow_up")
        if not self.can_resume and queue:
            operation_id = self._launch_turn("queued", queued=queue[0])
            if prompt.strip() or (images if images is not None else self.state.pending_images):
                self._submit_input(prompt, images, "steer")
            return operation_id
        return self._launch_turn("resume" if self.can_resume else "send", prompt, images)

    @property
    def questions(self) -> tuple[AskedQuestion, ...]:
        return tuple(self._questions.values())

    def answer_question(self, key: tuple[str, str, str], answer: str) -> str:
        """问题卡片独立于输入草稿；运行中投递补充，结束后发送正常新轮次。"""
        item = self._questions.get(key)
        if item is None or item.answer is not None or item.sending:
            raise ValueError("问题已回答、正在发送或已不在当前对话中。")
        if self.state.input_protected or self._save_error is not None:
            raise ValueError("请先处理会话保存或读取错误；回答草稿保留。")
        message = reply_message(item, answer)
        if self.busy and not self.can_supplement:
            raise ValueError("当前操作暂不接收回答，请稍后再次发送。")
        if not self.busy and not self.can_resume:
            queue = self.conversation.pending_inputs("follow_up")
            if queue:
                self._launch_turn("queued", queued=queue[0])
        if self.busy:
            submission = ConversationInput(uuid4().hex, "steer", message, self.state.operation_id)
            worker = self._workers[self.state.operation_id]
            if not worker.control.submit(submission):
                raise ValueError("原运行已结束，回答尚未接纳，请再次发送。")
            identifier = submission.input_id
            self._set_state(inputs=(*self.state.inputs, submission))
        else:
            identifier = self._launch_turn("resume" if self.can_resume else "send", message.text, [],
                                           message=message, preserve_draft=True)
        self._question_submissions[identifier] = (message.question_reply, self.state.operation_id)
        self._questions[key] = replace(item, sending=True)
        self.questions_changed.emit()
        return identifier

    def _settle_question_reply(self, identifier: str, accepted: bool) -> None:
        submission = self._question_submissions.pop(identifier, None)
        if submission is None:
            return
        reply, _ = submission
        key = reply.run_id, reply.call_id, reply.question_id
        if key in self._questions:
            self._questions[key] = replace(self._questions[key], sending=False,
                                            answer=reply.answer if accepted else None)
            self.questions_changed.emit()

    def queue_message(self, prompt: str, images: list[ImageContent] | None = None) -> str:
        return self._submit_input(prompt, images, "follow_up")

    def _submit_input(self, prompt: str, images: list[ImageContent] | None, kind: str) -> str:
        if not self.can_supplement or self.state.input_protected:
            raise ValueError("当前任务已结束或不接收补充；草稿保留。")
        references = tuple(images) if images is not None else self.state.pending_images
        self.set_draft(prompt, list(references))
        if self._submitted is not None and self._submitted[1] == self._draft_revision:
            return self._submitted[0]
        # 同一版尚未确认的草稿只投递一次，连续点击不生成重复输入。
        existing = next((key for key, revision in self._submissions.items() if revision == self._draft_revision), None)
        if existing is not None:
            return existing
        options = self.state.turn_options or self.conversation.last_options
        if kind == "follow_up":
            options.validate(self.conversation.settings)
        # 界面只捕获原文；技能发现与正文快照由后台接纳输入时完成。
        message = user_message(prompt, list(references))
        item = ConversationInput(uuid4().hex, kind, deepcopy(message),
                                 self.state.operation_id, options=options if kind == "follow_up" else None)
        self._draft_submission, self._draft_dirty = item.input_id, True
        if not self.flush_drafts():
            raise ValueError("请先处理草稿保存错误，消息尚未投递。")
        worker = self._workers[self.state.operation_id]
        self._submissions[item.input_id] = self._draft_revision
        self._set_state(inputs=(*self.state.inputs, item))
        if not worker.control.submit(item):
            self._submissions.pop(item.input_id)
            self._set_state(inputs=tuple(value for value in self.state.inputs if value.input_id != item.input_id))
            raise ValueError("原运行已经结束，输入没有接纳；请再次发送草稿。")
        return item.input_id

    def _launch_turn(self, kind: str, prompt: str = "", images: list[ImageContent] | None = None,
                     queued: ConversationInput | None = None, regeneration_inputs=None,
                     message=None, preserve_draft: bool = False) -> str:
        conversation_id = self._current_id()
        tools = self._tools
        run_id = uuid4().hex
        control = ConversationControl(conversation_id, run_id)
        record_requests = self.state.record_next_turn
        references = tuple(images) if images is not None else self.state.pending_images
        if queued is not None:
            prompt, references = queued.message.text, tuple(part for part in queued.message.content if isinstance(part, ImageContent))
        if message is None:
            message = (queued.message if queued is not None else user_message(
                prompt, list(references)) if prompt.strip() or references else None)
        # 在启动前捕获不可变选项；后续滑条操作不能改变运行、工具循环或重试。
        options = queued.options if queued is not None else self.state.turn_options or self.conversation.last_options
        options = deepcopy(options)
        if options.model_target is None:
            options = replace(options, model_target=self.conversation.settings.model_target)
        from ...llm.reasoning import is_disabled_effort
        if queued is None and is_disabled_effort(options.model_target.config.reasoning, options.reasoning_effort):
            self.set_reasoning_effort(None)
            options = self.state.turn_options
        self._set_state(run_target=options.model_target)

        previous = self.conversation.resumable_turn.run_id if kind == "resume" else None
        async def send(notify):
            arguments = dict(tool_names=tools, run_id=run_id, on_event=notify, options=options,
                             record_requests=record_requests, on_record=notify, control=control)
            if queued is not None:
                return await self.application.send_queued(conversation_id, queued.input_id, **arguments)
            if previous is not None:
                return await self.application.resume_message(conversation_id, previous, prompt, images=list(references), message=message, **arguments)
            return await self.application.send_message(conversation_id, prompt, images=list(references),
                                                       regeneration_inputs=regeneration_inputs, message=message, **arguments)

        operation_id = self._start(kind, send, pending_input=prompt if queued is None and not preserve_draft else None,
                                  pending_images=references, operation_id=run_id, control=control,
                                  queued=queued, live_prompt=prompt, preserve_draft=preserve_draft,
                                  submitted_message=message)
        if record_requests:
            self._set_state(record_next_turn=False, record_warning=None)
        return operation_id

    def set_record_next_turn(self, enabled: bool) -> None:
        """一次性内存开关；运行中选择只影响下一轮，不改模型设置或正式日志。"""
        self._set_state(record_next_turn=enabled)

    def set_reasoning_effort(self, effort: str | None) -> None:
        if self.state.input_protected:
            raise ValueError("本轮选项受保护，请先处理读取错误。")
        self._current_id()
        options = replace(self.state.turn_options or self.conversation.last_options, reasoning_effort=effort)
        options.validate(self.conversation.settings)
        if options == self.state.turn_options:
            return
        self._options_dirty = True
        self._set_state(turn_options=options, options_error=None)
        self._options_timer.start()

    def set_model_target(self, target: ModelTarget, effort: str | None) -> None:
        """只改变本会话下一独立运行；活动run及已排队项持有各自快照。"""
        if self.state.input_protected:
            raise ValueError("下一轮选项受保护，请先处理读取错误。")
        self._current_id()
        options = replace(self.state.turn_options or self.conversation.last_options, reasoning_effort=effort, model_target=target)
        options.validate(self.conversation.settings)
        if options == self.state.turn_options:
            return
        self._options_dirty = True
        self._set_state(turn_options=options, options_error=None)
        self._options_timer.start()

    @Slot()
    def flush_turn_options(self) -> bool:
        """合并拖动产生的连续选择；发送与关闭前确认保存失败可重试。"""
        self._options_timer.stop()
        if self.state.input_protected and not self._options_dirty:
            return True
        if not self._options_dirty:
            return self.state.options_error is None
        try:
            self.application.save_turn_options(self.conversation, self.state.turn_options)
        except (OSError, ValueError) as exc:
            self._set_state(options_error=f"下一轮模型／强度选择尚未保存：{exc}")
            return False
        self._options_dirty = False
        self._set_state(options_error=None)
        return True

    def set_draft(self, text: str, images: list[ImageContent] | None = None) -> None:
        """未来输入框的文本变化入口；不会把草稿追加到模型消息。"""
        if self.state.input_protected:
            raise ValueError("草稿受保护，请先处理读取错误。")
        if not isinstance(text, str):
            raise ValueError("输入草稿须为文本。")
        self._current_id()
        references = tuple(images) if images is not None else self.state.pending_images
        if any(not isinstance(part, ImageContent) for part in references):
            raise ValueError("输入附件须为 ImageContent 引用。")
        if self.state.pending_input == text and self.state.pending_images == references:
            return
        self._draft_submission = None
        self._draft_dirty = True
        self._draft_revision += 1
        self._set_state(pending_input=text, pending_images=references)
        self._draft_timer.start()

    def attach_images(self, paths: list[Path]) -> str:
        """文件选择固定到本会话；完成通知不会改变其他页面的草稿。"""
        self._current_id()
        selected = list(paths)
        if not selected or len(selected) + len(self.state.pending_images) > MAX_REQUEST_IMAGES:
            raise ValueError("请选择图片，当前草稿最多接收 8 张。")

        async def import_selected(_):
            return await self.application.attachments.import_files(selected)

        return self._start("attach", import_selected)

    @Slot()
    def flush_drafts(self) -> bool:
        """连续输入合并写入；切换和关闭前主动刷新，失败保留内存与候选文件。"""
        self._draft_timer.stop()
        if self.state.input_protected and not self._draft_dirty:
            return True
        if not self._draft_dirty:
            return self.state.draft_error is None
        try:
            self.application.drafts.save(self._current_id(), self.state.pending_input, list(self.state.pending_images),
                                         submitted_input_id=self._draft_submission,
                                         navigation_id=self.conversation.navigation["operation_id"] if self.conversation.navigation else None)
        except (OSError, ValueError) as exc:
            self._set_state(draft_error=f"草稿尚未保存：{exc}")
            return False
        self._draft_dirty = False
        self._set_state(draft_error=None)
        return True

    def compact_unavailable(self) -> str | None:
        if self.conversation is None:
            return "请先打开已有对话。"
        if self.busy:
            return "请等当前操作结束，或先手动停止；压缩不会中断它。"
        if self._save_error is not None or self.state.phase == "save_failed":
            return "请先保存当前会话结果。"
        if self.state.input_protected or self.state.draft_error or self.state.options_error:
            return "请先处理草稿或选项读取／保存错误。"
        ended = sum(turn.finished_at is not None for turn in self.conversation.turns)
        previous = self.conversation.latest_compaction
        if ended <= (previous.covered_turns if previous is not None else 0):
            return "没有新的已结束历史可压缩。"
        return None

    def compact(self) -> str:
        reason = self.compact_unavailable()
        if reason:
            raise ValueError(reason)
        conversation_id, tools = self._current_id(), self._tools
        run_id, record_requests = uuid4().hex, self.state.record_next_turn
        options = deepcopy(self.state.turn_options or self.conversation.last_options)
        if not self.flush_turn_options():
            raise ValueError("请先处理下一轮选项保存错误。")
        self._set_state(run_target=options.model_target or self.conversation.settings.model_target)

        async def summarize(notify):
            return await self.application.compact(conversation_id, tool_names=tools, on_event=notify,
                run_id=run_id, record_requests=record_requests, on_record=notify, options=options)

        operation_id = self._start("compact", summarize, operation_id=run_id)
        if record_requests:
            self._set_state(record_next_turn=False, record_warning=None)
        return operation_id

    def refresh_environment(self, overrides: dict[str, str] | None = None) -> str:
        paths = dict(overrides) if overrides is not None else None

        async def refresh(_):
            return self.application.environment.refresh(paths)

        return self._start("environment", refresh)

    def retry_save(self) -> str:
        if self._save_error is None or self.conversation is None:
            raise ValueError("没有可以重试的会话保存。")
        target = self.state.run_target or self.conversation.last_options.model_target
        failure, config, tools = self._save_error, target.config, self._tools

        async def save(_):
            return self.application.retry_save(failure, config, tool_names=tools)

        return self._start("retry_save", save)

    def cancel(self, operation_id: str | None = None) -> bool:
        if operation_id is None:
            if len(self._operations) != 1:
                return False
            operation_id = next(iter(self._operations))
        if operation_id not in self._operations:
            return False
        # 请求终态到线程退出之间，取消可能已来不及；停止意图仍须拦住后续自动队列。
        self._queue_paused = True
        if self.application.subagents is not None and self._conversation_id is not None:
            self.application.subagents.stop_parent(self._conversation_id)
        if not self.tasks.cancel(operation_id):
            self.state_changed.emit(self.state)
            return False
        if self.state.operation_id == operation_id:
            self._set_state(phase="cancelling")
        return True

    def close_block(self) -> CloseBlock | None:
        if self.busy:
            return CloseBlock("后台仍在运行", "请先取消当前操作，并等待后台退出后再关闭。")
        if self._save_error is not None:
            return CloseBlock("会话保存未确认", "请先重试保存或处理原日志；不要通过重跑任务补存。")
        if self.state.input_protected:
            return None
        if not self.flush_drafts():
            return CloseBlock("草稿尚未保存", self.state.draft_error)
        if not self.flush_turn_options():
            return CloseBlock("强度选择尚未保存", self.state.options_error)
        return None

    def try_subagent_wake(self):
        """真实子结果开启程序轮次；FIFO优先，不消费或改写当前草稿。"""
        manager = self.application.subagents
        if (manager is None or self.conversation is None or self.busy or self._save_error is not None
                or self.state.phase != "completed"
                or self._queue_paused or self.revising or self.can_resume or self.state.input_protected
                or self.state.draft_error or self.state.options_error or self.conversation.pending_inputs("follow_up")):
            return False
        events = manager.notices(self._conversation_id, wake=True)
        if not events:
            return False
        if not self.flush_turn_options():
            return False
        options = deepcopy(self.state.turn_options or self.conversation.last_options)
        run_id = uuid4().hex
        control = ConversationControl(self._conversation_id, run_id)
        self._set_state(run_target=options.model_target or self.conversation.settings.model_target)
        async def run(notify):
            return await self.application.send_program(self._conversation_id, events, tool_names=self._tools,
                run_id=run_id, on_event=notify, options=options, control=control)
        try:
            self._start("program", run, operation_id=run_id, control=control)
        except (OSError, ValueError) as exc:
            self._set_state(error=f"子结果保留，自动续接未开始：{exc}")
            return False
        return True

    def _current_id(self) -> str:
        if self.conversation is None:
            raise ValueError("请先创建或打开会话。")
        return self.conversation.conversation_id

    @property
    def revision_boundary(self) -> CompactionRecord | CompactionStatus | None:
        """成功通知先于整轮读回到达；界面操作立即采用同一持久边界。"""
        saved = self.conversation.latest_compaction if self.conversation is not None else None
        live = self._live_view.latest_compaction if self._live_view is not None else None
        return live if live is not None and (saved is None or live.finished_at > saved.finished_at) else saved

    def revision_unavailable(self) -> str | None:
        self._current_id()
        return COMPACTION_REVISION_ERROR if self.revision_boundary is not None else None

    def revise(self, anchor, *, replacement: str | None = None, replay_inputs=None) -> str:
        """确认后的意图只登记一次；先停止并结算原运行，再追加修订。"""
        identifier = self._current_id()
        if self._pending_revision is not None:
            return self._pending_revision[0]
        reason = self.revision_unavailable()
        if reason:
            raise ValueError(reason)
        if not self.flush_drafts() or not self.flush_turn_options():
            raise ValueError("请先处理草稿或选项保存错误。")
        operation_id = uuid4().hex
        from dataclasses import asdict
        draft = {"text": self.state.pending_input, "images": [asdict(part) for part in self.state.pending_images]}
        options = deepcopy(self.state.turn_options or self.conversation.last_options)

        async def commit(_):
            return self.application.rewind(identifier, anchor, operation_id=operation_id, draft=draft,
                                           options=options, replacement=replacement)

        action = (operation_id, commit)
        self._revision_retry = action
        self._after_revision = deepcopy(replay_inputs)
        self._queue_paused = True
        if self.busy:
            if not self.run_is_active:
                raise ValueError("请等待当前保存操作结束。")
            self._pending_revision = action
            self.cancel(self.state.operation_id)
            return operation_id
        return self._start("revise", commit, operation_id=operation_id)

    def fork(self, anchor, *, title: str) -> str:
        identifier = self._current_id()
        existing = next((identifier for identifier, item in self._operations.items() if item.kind == "fork"), None)
        if existing:
            return existing
        operation_id, new_id = uuid4().hex, uuid4().hex
        options = deepcopy(self.state.turn_options or self.conversation.last_options)

        async def create_branch(_):
            return self.application.fork(identifier, anchor, new_id=new_id, operation_id=operation_id, options=options, title=title)

        # 分支在独立文件保存；原请求可继续，返回值不能替换原控制器归属。
        self._fork_retry = (operation_id, create_branch)
        return self._start("fork", create_branch, operation_id=operation_id)

    def retry_fork(self) -> str:
        if not self.can_retry_fork:
            raise ValueError("没有可补存的分支。")
        identifier, operation = self._fork_retry
        return self._start("fork", operation, operation_id=identifier)

    def apply_prompt(self, config, *, preset_id=None, preset_name=None) -> str:
        identifier = self._current_id()
        async def apply(_):
            return self.application.apply_prompt(identifier, config, preset_id=preset_id, preset_name=preset_name)
        return self._start("prompt", apply)

    def retry_revision(self) -> str:
        if self._revision_retry is None:
            raise ValueError("没有可补存的对话修订。")
        identifier, commit = self._revision_retry
        return self._start("revise", commit, operation_id=identifier)

    def regenerate(self, anchor) -> str:
        turn = next((turn for turn in self.conversation.turns if turn.run_id == anchor.run_id), None)
        if turn is None:
            raise ValueError("原运行已不在有效对话中。")
        # 暂停续接沿链回到真实用户目标，不能凭空造一个开始提示词。
        end = self.conversation.turns.index(turn)
        root = regeneration_anchor(self.conversation, anchor)
        turn = next(value for value in self.conversation.turns if value.run_id == root.run_id)
        start = self.conversation.turns.index(turn)
        ids = [identifier for item in self.conversation.turns[start:end + 1]
               for identifier in [*(value for step in item.steps for value in step.input_ids), *item.retained_input_ids]]
        by_id = {item.input_id: item.message for item in self.conversation.inputs}
        return self.revise(root, replay_inputs=[by_id[value] for value in dict.fromkeys(ids)])

    def retry_regeneration(self) -> str:
        if not self.can_retry_regeneration:
            raise ValueError("没有尚未启动的重新执行。")
        identifier = self._launch_turn("send", self.state.pending_input, list(self.state.pending_images),
                                       regeneration_inputs=self._after_revision)
        self._after_revision = None
        return identifier

    def restore_displaced_draft(self) -> None:
        previous = (self.conversation.navigation or {}).get("displaced_draft")
        if not previous:
            raise ValueError("没有回退前的草稿。")
        from ...llm.messages import content_from_dict
        self.set_draft(previous["text"], [content_from_dict(part) for part in previous["images"]])
        self.flush_drafts()

    def _start(self, kind: str, operation: Operation, *, pending_input: str | None = None,
               operation_id: str | None = None, tool_names: tuple[str, ...] | None = None,
               pending_images: tuple[ImageContent, ...] | None = None,
               control: ConversationControl | None = None, queued: ConversationInput | None = None,
               live_prompt: str | None = None, preserve_draft: bool = False,
               submitted_message=None) -> str:
        auxiliary = kind == "fork" or kind == "attach" and self.can_supplement
        if (self.busy and not auxiliary) or (self._save_error is not None and kind != "retry_save"):
            raise ValueError("已有操作仍在运行或会话待保存，请先完成它。")
        if self.state.input_protected and kind not in {"open", "environment"}:
            raise ValueError("草稿或选项读取失败；请重新读取，历史记录保持不变。")
        if pending_input is not None:
            self.set_draft(pending_input, list(pending_images) if pending_images is not None else None)
        if kind in {"send", "resume"} and not preserve_draft and (pending_input or pending_images):
            self._draft_submission, self._draft_dirty = operation_id, True
        if kind in {"create", "open", "send", "resume"} and not self.state.input_protected and not self.flush_drafts():
            raise ValueError("请先处理草稿保存错误，再发送或切换会话。")
        if kind in {"send", "resume"} and not self.flush_turn_options():
            raise ValueError("请先处理强度选择保存错误，再发送消息。")
        worker = ConversationWorker(operation, self, operation_id=operation_id, control=control)
        workspace = None
        if kind in {"send", "resume", "queued", "program", "compact", "retry_save"} and self.conversation is not None:
            workspace = Path(self.conversation.settings.workspace) if self.conversation.settings.workspace is not None else None
        try:
            self.tasks.register(worker.operation_id, kind, self._conversation_id, worker.request_cancel, workspace=workspace)
        except Exception:
            worker.deleteLater()
            raise
        worker.progress.connect(self._on_event)
        worker.finished.connect(self._on_finished)
        self._operations[worker.operation_id] = _OperationState(kind, tool_names, self._draft_revision)
        self._workers[worker.operation_id] = worker
        previous_view, previous_submission = self._live_view, self._submitted
        if kind in {"send", "resume", "queued"}:
            self._queue_paused = False
        if kind == "resume" and (submitted_message is not None or pending_input or pending_images):
            resumed_input = ConversationInput(worker.operation_id, "steer", submitted_message or user_message(pending_input or "", list(pending_images or ())),
                                             worker.operation_id)
            self.state = replace(self.state, inputs=(*self.state.inputs, resumed_input))
        if kind in {"send", "resume", "queued", "program", "compact"}:
            self._live_view = LiveRunView(worker.operation_id, "send" if kind == "queued" else kind,
                                         live_prompt if live_prompt is not None else pending_input or "", pending_images or (),
                                         source_input_id=queued.input_id if queued else None)
            for item in self.state.inputs:
                self._live_view.record_input(item)
        if kind in {"send", "resume"} and not preserve_draft:
            self._submitted = (worker.operation_id, self._draft_revision)
        if not auxiliary:
            self._set_state(phase="running", result=None, error=None, diagnostics=None, operation_id=worker.operation_id)
        else:
            self.state_changed.emit(self.state)
        try:
            worker.start()
        except Exception as exc:
            self._operations.pop(worker.operation_id)
            self._workers.pop(worker.operation_id)
            self.tasks.release(worker.operation_id)
            self._live_view, self._submitted = previous_view, previous_submission
            self.state = replace(self.state, inputs=tuple(item for item in self.state.inputs if item.input_id != worker.operation_id))
            worker.deleteLater()
            changes = dict(error="后台线程未能启动；此操作没有执行。", diagnostics=exception_diagnostics(exc))
            if not auxiliary:
                changes["phase"] = "failed"
            self._set_state(**changes)
            self.task_finished.emit(worker.operation_id)
            raise
        return worker.operation_id

    @Slot(object)
    def _on_event(self, event: ConversationEvent) -> None:
        operation = self._operations.get(event.operation_id)
        if operation is None:
            return
        kind = operation.kind
        if kind == "fork":
            if event.kind == "completed":
                self._fork_retry = None
                self.branch_created.emit(event.data)
            elif event.kind == "failed":
                self._set_state(error=event.error, diagnostics=event.diagnostics)
            if event.kind in {"completed", "cancelled", "failed"}:
                self.tasks.update_phase(event.operation_id, event.kind)
            return
        if event.kind == "input" and isinstance(event.data, InputNotice):
            notice = event.data
            self._settle_question_reply(notice.item.input_id, notice.accepted)
            revision = self._submissions.pop(notice.item.input_id, None)
            if notice.accepted:
                # 投影更新用编号复用同一个普通气泡；没有等待或排队序号标签。
                values = tuple(notice.item if value.input_id == notice.item.input_id else value for value in self.state.inputs)
                if self._live_view is not None:
                    self._live_view.record_input(notice.item)
                self._set_state(inputs=values)
                if revision is not None and self._draft_revision == revision:
                    self.set_draft("", [])
                    self.flush_drafts()
            else:
                self._set_state(inputs=tuple(value for value in self.state.inputs if value.input_id != notice.item.input_id), error=notice.error)
            self.event_received.emit(replace(event, conversation_id=self._conversation_id))
            return
        if kind == "attach":
            if event.kind == "completed":
                references = tuple(info.as_content() for info in event.data)
                if len(self.state.pending_images) + len(references) <= MAX_REQUEST_IMAGES:
                    self.set_draft(self.state.pending_input, [*self.state.pending_images, *references])
                    self.flush_drafts()
                else:
                    self._set_state(error="当前草稿已达到8张图片，导入文件仍保留。")
            elif event.kind == "failed":
                self._set_state(error=event.error, diagnostics=event.diagnostics)
            if event.kind in {"completed", "cancelled", "failed"}:
                self.tasks.update_phase(event.operation_id, event.kind)
                if self.state.operation_id == event.operation_id:
                    self._set_state(phase="idle" if event.kind == "completed" else event.kind)
            self.event_received.emit(replace(event, conversation_id=self._conversation_id))
            return
        if event.kind in {"completed", "cancelled", "failed"} and self._live_view is not None and self._live_view.operation_id == event.operation_id:
            self._live_view.finish()
        if event.kind == "request_record" and isinstance(event.data, RequestRecordNotice):
            self._set_state(record_run_id=event.data.run_id if event.data.available else self.state.record_run_id,
                            record_warning=event.data.warning)
        elif event.kind == "agent":
            if isinstance(event.data, AgentEvent) and event.data.kind == "model_started":
                # 该通知在开始检查点保存之后发出，用户消息已经进入正式历史。
                self._clear_submitted(event.operation_id)
                self._settle_question_reply(event.operation_id, True)
            if isinstance(event.data, AgentEvent) and event.data.kind == "tool_finished":
                published = questions_from_result(event.data.result)
                for question in published:
                    item = AskedQuestion(event.operation_id, event.data.result.call_id, question)
                    self._questions.setdefault(item.key, item)
                if published:
                    self.questions_changed.emit()
            if self.state.phase == "cancelling" and isinstance(event.data, AgentEvent) and event.data.kind == "text_delta":
                return
            if self._live_view is not None and isinstance(event.data, AgentEvent):
                self._live_view.record(event.data)
        elif event.kind == "completed":
            self._accept_update(event.data, event.operation_id)
        elif event.kind == "cancelled":
            if isinstance(event.data, ConversationUpdate):
                self._accept_update(event.data, event.operation_id)
                self._set_state(phase="cancelled")
            else:
                self._set_state(phase="cancelled", result=event.data)
        elif event.kind == "failed":
            if isinstance(event.data, (ConversationSaveError, CompactionSaveError)):
                self._save_error = event.data
            self._set_state(phase="save_failed" if self._save_error is not None else "failed",
                            result=getattr(event.data, "result", None), error=event.error, diagnostics=event.diagnostics)
        if event.kind in {"completed", "cancelled", "failed"}:
            phase = "completed" if self.state.phase == "idle" else self.state.phase
            self.tasks.update_phase(event.operation_id, phase)
        self.event_received.emit(replace(event, conversation_id=self._conversation_id))

    def _accept_update(self, update: ConversationUpdate | EnvironmentSnapshot, operation_id: str) -> None:
        if isinstance(update, EnvironmentSnapshot):
            self.environment_changed.emit(update)
            # 清单刷新不自动恢复任务；下一次发送仍核对当前会话固定的选项。
            self._set_state(phase="idle", environment=None)
            return
        if update.conversation is not None:
            operation = self._operations[operation_id]
            if operation.tool_names is not None:
                self._tools = operation.tool_names
            self.conversation = update.conversation
            self._questions = collect_questions(update.conversation)
            self.questions_changed.emit()
            submitted = self._draft_submission
            if submitted is not None and (any(item.input_id == submitted for item in update.conversation.inputs)
                                          or any(turn.run_id == submitted for turn in update.conversation.turns)):
                self.set_draft("", [])
                self.flush_drafts()
            self._conversation_id = update.conversation.conversation_id
            self.tasks.bind_conversation(operation_id, self._conversation_id)
            self._live_view = None
            self.state = replace(self.state, inputs=update.conversation.pending_inputs())
            if operation.kind in {"create", "open", "revise", "prompt"}:
                self.state = replace(self.state, turn_options=update.turn_options or update.conversation.last_options, options_error=update.options_error,
                    draft_error=update.draft_error, input_protected=bool(update.draft_error or update.options_error),
                    record_run_id=update.record_run_id, record_warning=update.record_warning)
            self.conversation_changed.emit(update.conversation)
            if operation.kind in {"create", "open", "revise", "prompt"} and self._draft_revision == operation.draft_revision:
                # 读回期间输入已变更时保留新草稿，不能被较早读取的内容覆盖。
                self._set_state(pending_input=update.draft or "", pending_images=update.draft_images)
                self._draft_submission, self._submitted, self._draft_dirty = None, None, False
            if self._submitted is not None and any(
                turn.run_id == self._submitted[0] for turn in update.conversation.turns
            ):
                # 请求前取消／失败可能没有模型事件；读回也能确认消息已记录。
                self._clear_submitted(self._submitted[0])
        self._save_error = None
        result = update.result
        if isinstance(result, AgentResult):
            phase = {"completed": "completed", "cancelled": "cancelled", "failed": "failed"}.get(result.stop_reason, "stopped")
            self._set_state(phase=phase, result=result, error=result.error, diagnostics=result.diagnostics,
                            environment=update.environment)
        elif isinstance(result, CompactionRecord):
            self._set_state(phase="completed" if result.status == "succeeded" else "stopped", result=result,
                            error=result.error, environment=update.environment)
        else:
            self._set_state(phase="idle", environment=update.environment)
        if self._operations[operation_id].kind == "revise":
            self._revision_retry = None

    @Slot()
    def _on_finished(self) -> None:
        worker = self.sender()
        if not isinstance(worker, ConversationWorker) or worker.operation_id not in self._operations:
            return
        operation = self._operations.pop(worker.operation_id)
        unresolved = [identifier for identifier, (_, origin) in self._question_submissions.items()
                      if origin == worker.operation_id]
        for identifier in unresolved:
            self._settle_question_reply(identifier, False)
        if unresolved:
            self.state = replace(self.state, inputs=tuple(item for item in self.state.inputs
                                 if item.input_id not in unresolved))
        self._workers.pop(worker.operation_id)
        self.tasks.release(worker.operation_id)
        worker.deleteLater()
        # 再次通知界面，此时才可以开始下一项或关闭窗口。
        self.state_changed.emit(self.state)
        self.task_finished.emit(worker.operation_id)
        if not self.busy and self._pending_revision is not None:
            if self._save_error is not None:
                self._set_state(error="原运行保存未确认，请先重试保存，再提交回退。")
                return
            identifier, commit = self._pending_revision
            self._pending_revision = None
            try:
                self._start("revise", commit, operation_id=identifier)
            except (OSError, ValueError) as exc:
                self._set_state(error=f"原运行已停止，修订尚未开始：{exc}")
            return
        if operation.kind == "revise" and self._revision_retry is None and self._after_revision is not None:
            try:
                self.retry_regeneration()
            except (OSError, ValueError) as exc:
                self._set_state(error=f"对话已回退，重新执行尚未开始，输入保留：{exc}")
            return
        # 只有旧线程真实退出并释放目录后，才能启动一个新的FIFO轮次。
        result = self.state.result
        if operation.kind in {"send", "resume", "queued", "program", "attach", "fork"} and not self.busy and not self._queue_paused and self._save_error is None and (
                isinstance(result, AgentResult) and result.stop_reason == "completed" and self.state.phase == "completed"
                and self.state.draft_error is None and self.state.options_error is None and not self.state.input_protected):
            queue = self.conversation.pending_inputs("follow_up")
            if queue:
                try:
                    self._launch_turn("queued", queued=queue[0])
                except (OSError, ValueError) as exc:
                    self._set_state(error=f"排队消息保留，尚未开始：{exc}")
        if not self.busy:
            self.try_subagent_wake()

    def _set_state(self, **changes) -> None:
        changes.setdefault("conversation_id", self._conversation_id)
        self.state = replace(self.state, **changes)
        self.state_changed.emit(self.state)

    def _clear_submitted(self, run_id: str) -> None:
        if self._submitted is None or self._submitted[0] != run_id:
            return
        _, revision = self._submitted
        self._submitted = None
        # 运行期间新输入不能被上一轮的完成通知清空。
        if self._draft_revision == revision:
            self.set_draft("", [])
            self.flush_drafts()
