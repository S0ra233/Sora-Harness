"""把新用户消息交给现有 Agent 循环；恢复和保存重试都不重做旧工具。"""
import asyncio
from dataclasses import asdict, replace
from pathlib import Path
from uuid import UUID, uuid4

from ...environment.models import EnvironmentCheck
from ...llm.contracts import TextSink
from ...llm.messages import ImageContent, ModelMessage, user_message
from ...llm.models import ModelConfig, ModelRequest
from ...llm.diagnostics import RequestTraceFactory
from ...llm.validation import resolve_model_config
from ...prompts import load_summary_instructions
from ...llm.providers.http import ProviderError
from ...llm.providers.image_input import validate_image_input
from ..agent.models import AgentCancelled, AgentCheckpoint, AgentEventSink, AgentInputBatch, AgentLimits, AgentResult
from ..application import Runtime
from ..contracts import RunPreparation
from ..runs import exception_diagnostics
from ..context.compaction import ConversationCompactor, recover_compaction
from ..context.budget import effective_window
from ...llm.model_catalog import ModelTarget
from .history import build_history, interrupted_result
from .models import CompactionRecord, Conversation, ConversationInput, ConversationSettings, TurnOptions
from .control import ConversationControl
from .store import ConversationJournal, ConversationStore
from ...tools.command.shells import ShellConfig, resolve_shell
from ...tools.filesystem.search_config import SEARCH_TOOLS, SearchConfig, resolve_search_config


class ConversationSaveError(OSError):
    """保留本轮实际结果和待存检查点；长期运行入口可以仅重试落盘。"""

    def __init__(self, conversation_id: str, run_id: str, result: AgentResult, pending: dict | None) -> None:
        super().__init__(f"会话 {conversation_id}／运行 {run_id} 保存失败；已停止推进，请仅重试保存。")
        self.conversation_id, self.run_id = conversation_id, run_id
        self.result, self.pending = result, pending


class CompactionSaveError(OSError):
    """摘要保存失败携带原事件；重试只补存或结算，不再次请求摘要。"""

    def __init__(self, conversation_id: str, compaction_id: str | None, pending: dict | None) -> None:
        super().__init__(f"会话 {conversation_id} 的摘要保存未确认；请仅重试保存，不重新请求摘要。")
        self.conversation_id, self.compaction_id, self.pending = conversation_id, compaction_id, pending


class ConversationService:
    def __init__(
        self, runtime: Runtime, store: ConversationStore, config: ModelConfig, *, tool_names: list[str],
        preparation: RunPreparation | None = None,
    ) -> None:
        self._runtime, self._store = runtime, store
        self._config = resolve_model_config(config)
        # 这是应用本轮允许的集合，不能由日志中的工具名自行扩大。
        self._allowed_tools = frozenset(tool_names)
        self._preparation = preparation if preparation is not None else RunPreparation()
        self._run_started = False

    @property
    def enabled_plugins(self) -> frozenset[str] | None:
        """返回本服务所属运行的开关快照，不重读活动中的设置。"""
        return self._runtime.enabled_plugins

    @property
    def mcp_snapshot(self):
        return self._runtime.mcp_servers

    def prepare_run(self, preparation: RunPreparation) -> None:
        """请求开始前合并应用能力；运行中不能改工具范围或通知归属。"""
        if self._run_started:
            raise ValueError("运行已开始，不能修改本轮能力准备。")
        previous, binding = self._preparation, preparation.bind_tools
        if previous.bind_tools is not None and binding is not None:
            base = previous.bind_tools
            binding = lambda context: preparation.bind_tools(base(context))
        self._preparation = replace(preparation,
            additional_tool_names=tuple(dict.fromkeys((*previous.additional_tool_names, *preparation.additional_tool_names))),
            bind_tools=binding if binding is not None else previous.bind_tools,
            program_source=preparation.program_source or previous.program_source,
            program_ack=preparation.program_ack or previous.program_ack)

    def create(self, settings: ConversationSettings, *, environment: EnvironmentCheck | None = None) -> Conversation:
        self._check_settings(settings)
        if resolve_model_config(settings.config) != self._config:
            raise ValueError("创建会话的连接与当前后端不一致。")
        # 新会话保存本次后端实际使用的规则；旧会话头部不改写。
        settings.config = self._config
        if not settings.context_policy.summary_instructions.strip():
            settings = replace(settings, context_policy=replace(settings.context_policy,
                                summary_instructions=load_summary_instructions()))
        if "run_command" in settings.tool_names and environment is None:
            # 新会话固定实际解释器路径；继续时不能换到 PATH 中另一份同名程序。
            settings.shell_config = resolve_shell(settings.shell_config if settings.shell_config is not None else ShellConfig())
        if SEARCH_TOOLS.intersection(settings.tool_names) and environment is None:
            settings.search_config = resolve_search_config(settings.search_config if settings.search_config is not None else SearchConfig())
        return self._store.create(settings)

    def _recover(self, journal: ConversationJournal) -> None:
        recover_compaction(journal)
        if journal.conversation.active_turn is not None:
            previous = journal.conversation.active_turn
            self._persist_result(journal, previous.run_id, interrupted_result(previous))

    async def compact(
        self, conversation_id: str, *, on_event: AgentEventSink | None = None,
        environment: EnvironmentCheck | None = None,
        request_traces: RequestTraceFactory | None = None,
        options: TurnOptions | None = None,
    ) -> CompactionRecord:
        """手动摘要不创建用户轮次；与继续会话使用同一个排他写入范围。"""
        self._run_started = True
        journal = None
        record = None
        try:
            with self._store.writer(conversation_id) as journal:
                settings = journal.conversation.settings
                self._check_settings(settings)
                self._recover(journal)
                if options is not None:
                    options.validate(settings)
                    if options.model_target is not None and resolve_model_config(options.model_target.config) != self._config:
                        raise ValueError("摘要连接与选中的目标快照不一致。")
                messages = [ModelMessage("system", settings.system)] if settings.system else []
                messages.extend(build_history(journal.conversation, config=self._config))
                # 仅用于准备预算和固定上下文位置，不作为原始用户消息保存。
                messages.append(ModelMessage("user", "本次只整理历史摘要，不继续执行原任务。"))
                request, _ = self._runtime.prepare_agent_request(
                    ModelRequest(messages, settings.temperature, settings.max_output_tokens,
                                 reasoning_effort=options.reasoning_effort if options is not None else settings.reasoning_effort),
                    workspace=settings.workspace, tool_names=settings.tool_names,
                    prompt_config=options.prompt_config if options is not None and options.prompt_config is not None else journal.conversation.current_prompt,
                    prompt_mode=settings.prompt_mode,
                    runtime_baseline=journal.conversation.runtime_baseline,
                    context_policy=replace(settings.context_policy, context_window=effective_window(self._config.context_window, settings.context_policy.context_window)),
                    automatic_compaction=True,
                    preparation=self._preparation,
                    **self._environment_options(settings, environment),
                )
                compactor = ConversationCompactor(journal, self._config.api_style, on_event, request_traces=request_traces,
                    config=self._config, model_target=options.model_target if options is not None else None,
                    prompt_config=options.prompt_config if options is not None else journal.conversation.current_prompt)
                async with self._runtime.model_session() as backend:
                    await compactor.compact(request, backend, trigger="manual")
                    record = journal.conversation.compactions[-1]
            return record
        except ConversationSaveError:
            raise
        except OSError as exc:
            if journal is None:
                raise
            pending = journal.pending
            compaction_id = pending["run_id"] if pending is not None and pending["type"].startswith("compaction_") else (
                record.compaction_id if record is not None else None
            )
            raise CompactionSaveError(conversation_id, compaction_id, pending) from exc

    def retry_compaction_save(self, error: CompactionSaveError) -> CompactionRecord | None:
        """只确认原事件；开始已保存而无摘要结果时记中断，绝不重新生成。"""
        with self._store.writer(error.conversation_id) as journal:
            if error.pending is not None:
                journal.confirm_event(error.pending)
            if error.compaction_id is not None:
                if not journal.conversation.compactions or journal.conversation.compactions[-1].compaction_id != error.compaction_id:
                    raise ValueError("原压缩已不是最后一项摘要尝试，不能重试其保存。")
                recover_compaction(journal)
            journal.flush()
            return journal.conversation.compactions[-1] if error.compaction_id is not None else None

    def _check_settings(self, settings: ConversationSettings) -> None:
        if not set(settings.tool_names).issubset(self._allowed_tools):
            raise ValueError("会话记录包含本轮应用未授权的工具；记录不能自动扩大工具权限。")
        if settings.prompt_mode == "work":
            root = Path(settings.workspace)
            if not root.is_absolute() or not root.is_dir() or str(root.resolve(strict=True)) != settings.workspace:
                raise ValueError("会话工作目录不存在或实际路径已变化，请检查原目录。")

    @staticmethod
    def _environment_options(settings: ConversationSettings, environment: EnvironmentCheck | None) -> dict:
        """应用检测结果优先于创建时快照；检测缺失不等于禁止请求或开放工具。"""
        selected = environment if environment is not None else EnvironmentCheck(
            settings.shell_config, settings.search_config, settings.program_paths,
        )
        return {
            "shell_config": selected.shell, "search_config": selected.search,
            "program_paths": selected.program_paths,
            "environment_checked": environment is not None,
        }

    async def send_message(self, conversation_id: str, prompt: str, *, images: list[ImageContent] | None = None,
                           message: ModelMessage | None = None,
                           **options) -> AgentResult:
        return await self._run_turn(conversation_id, message if message is not None else user_message(prompt, images), **options)

    async def resume_message(self, conversation_id: str, expected_run_id: str, prompt: str = "", *,
                             images: list[ImageContent] | None = None, message: ModelMessage | None = None, **options) -> AgentResult:
        """新运行续接原目标；没有新文字时不虚构一条用户气泡。"""
        user = message if message is not None else user_message(prompt, images) if prompt.strip() or images else None
        return await self._run_turn(conversation_id, user, resumed_from_run_id=expected_run_id, **options)

    async def send_queued(self, conversation_id: str, input_id: str, **options) -> AgentResult:
        return await self._run_turn(conversation_id, None, source_input_id=input_id, **options)

    async def send_program(self, conversation_id: str, events: list[dict], **options) -> AgentResult:
        if not events:
            raise ValueError("程序续接需要真实子任务完成事件。")
        return await self._run_turn(conversation_id, None, program_events=events, **options)

    async def _run_turn(
        self, conversation_id: str, user: ModelMessage | None, *, run_id: str | None = None,
        limits: AgentLimits | None = None, on_text: TextSink | None = None,
        on_event: AgentEventSink | None = None,
        environment: EnvironmentCheck | None = None,
        options: TurnOptions | None = None,
        request_traces: RequestTraceFactory | None = None,
        control: ConversationControl | None = None,
        source_input_id: str | None = None,
        resumed_from_run_id: str | None = None,
        regeneration_inputs: list[ModelMessage] | None = None,
        program_events: list[dict] | None = None,
    ) -> AgentResult:
        """排他范围内恢复、追加、执行和保存；新消息才会启动模型调用。"""
        run_id = UUID(run_id).hex if run_id is not None else uuid4().hex
        result = None
        save_failure = None
        try:
            with self._store.writer(conversation_id) as journal:
                try:
                    result = await self._send_turn(journal, run_id, user, limits, on_text, on_event, environment, options, request_traces,
                                                   control, source_input_id, resumed_from_run_id, regeneration_inputs, program_events)
                except ConversationSaveError as exc:
                    result, save_failure = exc.result, exc
                    raise
                except AgentCancelled as exc:
                    result = exc.result
                    raise
                finally:
                    if control is not None:
                        control.close()
            return result
        except ConversationSaveError:
            raise
        except OSError as exc:
            if result is None:
                raise
            # 关闭文件或释放锁可能再次失败；仍保留原运行、事实和待存项。
            failed_run = save_failure.run_id if save_failure is not None else run_id
            pending = save_failure.pending if save_failure is not None else None
            raise ConversationSaveError(conversation_id, failed_run, result, pending) from exc

    async def _send_turn(
        self, journal: ConversationJournal, run_id: str, user: ModelMessage | None, limits: AgentLimits | None,
        on_text: TextSink | None, on_event: AgentEventSink | None,
        environment: EnvironmentCheck | None,
        options: TurnOptions | None,
        request_traces: RequestTraceFactory | None,
        control: ConversationControl | None,
        source_input_id: str | None,
        resumed_from_run_id: str | None,
        regeneration_inputs: list[ModelMessage] | None,
        program_events: list[dict] | None,
    ) -> AgentResult:
        self._run_started = True
        conversation = journal.conversation
        self._check_settings(conversation.settings)
        self._recover(journal)
        settings = conversation.settings
        if user is not None:
            from .questions import validate_question_reply
            validate_question_reply(conversation, user)
            user = self._runtime.skills.freeze(user, workspace=settings.workspace)
        if source_input_id is not None:
            queue = conversation.pending_inputs("follow_up")
            if conversation.resumable_turn is not None or not queue or queue[0].input_id != source_input_id:
                raise ValueError("队列只能在原目标正常结束后按接收顺序继续。")
            user, options = queue[0].message, queue[0].options
        continuation = None
        origins = {run_id}
        if resumed_from_run_id is not None:
            previous = conversation.resumable_turn
            if previous is None or previous.run_id != resumed_from_run_id:
                raise ValueError("暂停目标已经变化，不能继续另一个运行。")
            while previous is not None:
                origins.add(previous.run_id)
                previous = next((turn for turn in conversation.turns if turn.run_id == previous.resumed_from_run_id), None)
            continuation = ("[程序续接意图] 用户通过应用的继续操作恢复上一目标。沿用已保存的目标和事实；"
                            "不是额外的文件授权。已完成工具不要自动重跑，结果未知的操作先核对状态。")
        options = options if options is not None else TurnOptions(settings.reasoning_effort)
        target = options.model_target or settings.model_target
        if resolve_model_config(target.config) != self._config:
            raise ValueError("运行连接与提交的目标快照不一致，未发送请求。")
        options = replace(options, model_target=ModelTarget(self._config, target.model_id, target.connection_id,
                                                          target.label, target.connection_label),
                          prompt_config=options.prompt_config or conversation.current_prompt)
        options.validate(settings)
        resumed_input = user if resumed_from_run_id is not None else None
        messages = [ModelMessage("system", settings.system)] if settings.system else []
        messages.extend(build_history(conversation, config=self._config))
        if user is not None and resumed_from_run_id is None:
            messages.append(user)
        regeneration_notice = None
        if regeneration_inputs is not None:
            regeneration_notice = "[程序重新执行事实] 用户选择重新执行原目标。旧工具产生的文件和命令效果仍存在；本轮会生成新的调用，请按当前实际状态执行，不假定文件已回档。"
            messages.append(ModelMessage("user", regeneration_notice))
        if resumed_input is not None:
            validate_image_input(ModelRequest([resumed_input]), self._config)
        if continuation:
            messages.append(ModelMessage("user", continuation))
        request = ModelRequest(messages, settings.temperature, settings.max_output_tokens, reasoning_effort=options.reasoning_effort)
        validate_image_input(request, self._config)
        limits = limits if limits is not None else settings.limits
        first_compaction = len(conversation.compactions)
        policy = replace(settings.context_policy, context_window=effective_window(self._config.context_window, settings.context_policy.context_window))
        request, tool_context = self._runtime.prepare_agent_request(
            request, workspace=settings.workspace, tool_names=settings.tool_names,
            prompt_config=options.prompt_config or conversation.current_prompt, prompt_mode=settings.prompt_mode,
            context_policy=policy, automatic_compaction=True, runtime_baseline=conversation.runtime_baseline,
            preparation=self._preparation,
            **self._environment_options(settings, environment),
        )
        if tool_context is not None:
            if tool_context.reference_scope_id is None:
                tool_context = replace(tool_context, reference_scope_id=conversation.conversation_id)
            if tool_context.skills is not None:
                # 继续时的新输入及重新执行补充也会在第一请求边界进入，不只来自control投递。
                tool_context.skills.restore([*([user] if user is not None else []), *(regeneration_inputs or [])])
            # 压缩只改变模型视图；本地已执行调用的记录归属仍可从原日志核对。
            tool_context.artifact_calls.update(tool.call_id for turn in conversation.turns
                for step in turn.steps for tool in step.tools if tool.result is not None)
            if "read_tool_record" in tool_context.allowed_tools:
                from ...tools.records import issued_record_ids
                tool_context.artifact_ids.update(issued_record_ids((tool.result for turn in conversation.turns
                    for step in turn.steps for tool in step.tools if tool.result is not None), tool_context.artifacts_dir))
        try:
            data = {"user": asdict(user) if user is not None and source_input_id is None and resumed_from_run_id is None else None,
                    "limits": asdict(limits), "options": asdict(options)}
            if program_events:
                data.update(origin="subagent_notice", program_event_ids=[item["event_id"] for item in program_events])
            if regeneration_notice:
                data["regeneration_notice"] = regeneration_notice
            if source_input_id is not None:
                data["source_input_id"] = source_input_id
            if resumed_from_run_id is not None:
                data.update(resumed_from_run_id=resumed_from_run_id, continuation=continuation)
            if conversation.runtime_baseline is None:
                baseline = next(message for message in request.messages if isinstance(message, ModelMessage)
                                and message.runtime_context_kind == "baseline")
                data["runtime_baseline"] = asdict(baseline)
            if environment is not None:
                # 完整检测供审计；实际发出的环境基线／变化另随请求检查点恢复。
                data["execution_environment"] = asdict(environment)
            journal.append("turn_started", run_id, data)
            for message in regeneration_inputs or []:
                journal.append("input_submitted", run_id, {"input_id": uuid4().hex, "kind": "steer",
                               "message": asdict(message), "options": None})
            if resumed_input is not None:
                # 暂停期间的旧补充先进入下一请求，新写的文字排在它们之后，保持实际先后关系。
                journal.append("input_submitted", run_id, {"input_id": run_id, "kind": "steer",
                    "message": asdict(resumed_input), "options": None})
        except (OSError, ValueError) as exc:
            result = AgentResult(request, stop_reason="failed", error="本轮开始记录未保存，模型尚未调用。")
            raise ConversationSaveError(conversation.conversation_id, run_id, result, journal.pending) from exc

        compactor = ConversationCompactor(journal, self._config.api_style, on_event, request_traces=request_traces, config=self._config)

        def receive(item: ConversationInput) -> ConversationInput:
            if control.conversation_id != conversation.conversation_id or item.origin_run_id != run_id:
                raise ValueError("输入不属于这个会话的当前运行。")
            previous = next((value for value in conversation.inputs if value.input_id == item.input_id), None)
            if previous is not None:
                if not item.message.skills and previous.message.skills:
                    item = replace(item, message=replace(item.message, skills=previous.message.skills))
                if replace(previous, accepted_at="") != replace(item, accepted_at=""):
                    raise ValueError("相同输入编号的正文或选项不同。")
                return previous
            item = replace(item, message=self._runtime.skills.freeze(item.message, workspace=settings.workspace,
                scope=tool_context.skills if tool_context is not None else None))
            from .questions import validate_question_reply
            validate_question_reply(conversation, item.message)
            try:
                target_config = item.options.model_target.config if item.options is not None and item.options.model_target is not None else self._config
                if item.options is not None:
                    item.options.validate(settings)
                validate_image_input(ModelRequest([item.message]), target_config)
            except ProviderError as exc:
                # 收件能力错误只拒绝这条输入，不让回调异常吞掉确认或终止原目标。
                raise ValueError(str(exc)) from exc
            journal.append("input_submitted", run_id, {"input_id": item.input_id, "kind": item.kind,
                "message": asdict(item.message), "options": asdict(item.options) if item.options else None})
            if tool_context is not None and tool_context.skills is not None:
                tool_context.skills.restore([item.message])
            return conversation.inputs[-1]

        def inputs(final: bool) -> AgentInputBatch:
            if control is not None:
                control.drain()
                if control.failure is not None:
                    raise control.failure
            selected = tuple(item for item in conversation.pending_inputs("steer") if item.origin_run_id in origins)
            while final and not selected and control is not None:
                # 同一事件循环的最后边界关闭归属；迟到投递明确拒绝，不能漏进下一轮。
                if control.seal_if_empty():
                    break
                control.drain()
                if control.failure is not None:
                    raise control.failure
                selected = tuple(item for item in conversation.pending_inputs("steer") if item.origin_run_id in origins)
            used = conversation.consumed_program_ids
            source = self._preparation.program_source
            available = {item["event_id"]: item for item in [*(program_events or []), *(source() if source else [])]}
            notices = [item for identifier, item in available.items() if identifier not in used]
            messages = tuple(ModelMessage("user", item["message"], runtime_context_id=item["event_id"],
                            runtime_context_kind="subagent_notice") for item in notices)
            return AgentInputBatch(tuple(item.input_id for item in selected), (*tuple(item.message for item in selected), *messages),
                                   tuple(item["event_id"] for item in notices))

        def checkpoint(event: AgentCheckpoint) -> None:
            journal.append(event.kind, run_id, event.data, step=event.step)
            if event.kind == "model_started" and self._preparation.program_ack is not None:
                self._preparation.program_ack(event.data.get("program_event_ids", []))

        try:
            if control is not None:
                control.bind(receive)
            result = await self._runtime.run_prepared_agent(
                request, tool_context, limits=limits, on_text=on_text, on_event=on_event,
                on_checkpoint=checkpoint,
                before_request=compactor.before_request,
                request_traces=request_traces,
                input_source=inputs,
            )
        except AgentCancelled as exc:
            if control is not None:
                control.close()
            self._context_result(exc.result, journal, compactor, first_compaction)
            self._persist_result(journal, run_id, exc.result)
            raise
        except asyncio.CancelledError as exc:
            if control is not None:
                control.close()
            result = AgentResult(request, stop_reason="cancelled")
            self._context_result(result, journal, compactor, first_compaction)
            self._persist_result(journal, run_id, result)
            raise AgentCancelled(result) from exc
        except Exception as exc:
            result = AgentResult(
                request, stop_reason="failed",
                error=str(exc) if isinstance(exc, (ProviderError, OSError, ValueError)) else f"会话入口错误（{type(exc).__name__}）。",
                diagnostics=exception_diagnostics(exc),
            )
        if control is not None:
            control.close()
        self._context_result(result, journal, compactor, first_compaction)
        self._persist_result(journal, run_id, result)
        return result

    @staticmethod
    def _context_result(result: AgentResult, journal: ConversationJournal, compactor: ConversationCompactor, first: int) -> None:
        result.compactions = [asdict(record) for record in journal.conversation.compactions[first:]]
        # 最后一项结束事件可能已写入但尚未 fsync；已有摘要与实际用量随保存错误返回。
        if journal.pending is not None and journal.pending["type"] == "compaction_finished":
            pending = journal.pending
            if result.compactions and result.compactions[-1]["compaction_id"] == pending["run_id"]:
                result.compactions[-1].update(pending["data"])
        result.request_tokens_estimate = list(compactor.estimates)

    def _persist_result(self, journal: ConversationJournal, run_id: str, result: AgentResult) -> None:
        """只补齐检查点之后的新增事实；完整请求不再次复制进会话文件。"""
        try:
            if journal.pending is not None:
                raise journal.failure if journal.failure is not None else OSError("前一检查点尚未确认保存。")
            self._sync_steps(journal, run_id, result)
            journal.append("turn_finished", run_id, {
                "stop_reason": result.stop_reason, "error": result.error,
                "diagnostics": result.diagnostics, "usage_summary": result.usage_summary,
                "compaction_usage_summary": result.compaction_usage_summary,
                "total_usage_summary": result.total_usage_summary,
                "request_tokens_estimate": result.request_tokens_estimate,
            })
        except (OSError, ValueError) as exc:
            raise ConversationSaveError(journal.conversation.conversation_id, run_id, result, journal.pending) from exc

    @staticmethod
    def _sync_steps(journal: ConversationJournal, run_id: str, result: AgentResult) -> None:
        """失败和取消也结算片段、未执行或结果未知；已有结果不重复写入。"""
        turn = journal.conversation.active_turn
        if turn is None or turn.run_id != run_id:
            raise ValueError("待存结果没有对应的当前运行。")
        for step in result.steps:
            if len(turn.steps) < step.step:
                journal.append("model_started", run_id, {"context_messages": [asdict(message) for message in step.context_messages],
                                                         "input_ids": step.input_ids}, step=step.step)
            recorded = turn.steps[step.step - 1]
            if recorded.completion_status == "requesting":
                journal.append("model_finished", run_id, asdict(step), step=step.step)
                recorded = turn.steps[step.step - 1]
            for execution, saved in zip(step.tools, recorded.tools):
                if saved.result is None and execution.result is not None:
                    journal.append("tool_finished", run_id, asdict(execution), step=step.step)

    def retry_save(self, error: ConversationSaveError) -> AgentResult:
        """只确认或补写原检查点，再保存终态；不调用 Runtime、模型或工具。"""
        with self._store.writer(error.conversation_id) as journal:
            pending = error.pending
            if pending is not None:
                journal.confirm_event(pending)
            recover_compaction(journal)
            turn = journal.conversation.active_turn
            if turn is not None and turn.run_id == error.run_id:
                self._persist_result(journal, error.run_id, error.result)
            elif not journal.conversation.turns or journal.conversation.turns[-1].run_id != error.run_id:
                raise ValueError("原运行已不是会话最后一轮，不能重试其保存。")
            journal.flush()
            return error.result
