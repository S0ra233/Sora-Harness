"""检测环境并接入既有会话服务；缺失能力只作为事实，不阻止正常消息。"""
import asyncio
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from uuid import UUID, uuid4
from ..attachments.store import ImageStore
from ..credentials import resolve_api_key
from ..llm.messages import ImageContent, ModelMessage, TextContent
from ..llm.models import ModelConfig, ModelRequest
from ..llm.diagnostics import RequestRecordNotice, RequestTraceContext
from ..storage.request_records import RequestRecordStore
from ..runtime.context import estimate_request_tokens
from ..runtime.context.budget import effective_window
from ..runtime.conversations.history import build_history
from ..runtime.conversations.control import ConversationControl

from ..bootstrap import create_runtime
from ..environment import EnvironmentCheck, EnvironmentService
from ..llm.validation import resolve_model_config
from ..runtime.agent.models import AgentCancelled, AgentEventSink, AgentResult
from ..runtime.contracts import RunPreparation
from ..runtime.conversations import (
    CompactionRecord, CompactionSaveError, Conversation, ConversationSaveError,
    ConversationService, ConversationSettings, ConversationStore, ConversationSummary,
    TurnOptions,
)
from ..tools.command.shells import ShellConfig
from ..tools.filesystem.search_config import SEARCH_TOOLS, SearchConfig
from ..storage.paths import LocalPaths
from .drafts import DraftStore
from .turn_options import TurnOptionsStore
from .prompt_presets import PromptPresets
from .general_settings import GeneralSettings


@dataclass(frozen=True)
class ConversationUpdate:
    conversation: Conversation | None
    environment: EnvironmentCheck | None = None
    result: AgentResult | CompactionRecord | None = None
    draft: str | None = None
    draft_images: tuple[ImageContent, ...] = ()
    turn_options: TurnOptions | None = None
    draft_error: str | None = None
    options_error: str | None = None
    record_run_id: str | None = None
    record_warning: str | None = None


@dataclass(frozen=True)
class ConversationSummaryScan:
    summaries: tuple[ConversationSummary, ...]
    issues: tuple[str, ...]


class ConversationViewError(OSError):
    """运行事实已存在，界面读回失败不能通过重跑任务处理。"""

    def __init__(self, conversation_id: str, result: AgentResult | CompactionRecord | None) -> None:
        super().__init__(f"会话 {conversation_id} 的操作已停止，但读回失败；请重新打开原会话，不要重跑任务。")
        self.result = result


class ConversationCancelled(asyncio.CancelledError):
    def __init__(self, update: ConversationUpdate) -> None:
        super().__init__("会话操作已取消。")
        self.update = update


class ConversationApplication:
    """操作对象不保存活跃请求；同一会话的排他写入仍由 Store 管理。"""

    def __init__(self, local_directory: Path, *, tasks=None, enable_subagents: bool = True,
                 mutation_lock: Path | None = None, delegated_records=(), delegated_calls=(), excluded_env=(),
                 enabled_plugins=None, skill_roots=None, skill_manual_invocation=True,
                 human_interaction: bool = False, plugin_snapshot=None, mcp_snapshot=None,
                 reference_scope_id: str | None = None) -> None:
        self._reference_scope_id = reference_scope_id
        directory = local_directory.expanduser().resolve()
        paths = LocalPaths(directory)
        self.paths = paths
        from ..plugin_catalog import load_plugin_catalog
        from ..mcp.settings import McpSettings
        self.plugin_catalog = load_plugin_catalog(directory, snapshot=plugin_snapshot)
        self.mcp_settings = McpSettings(paths.mcp)
        self._mcp_snapshot = tuple(mcp_snapshot) if mcp_snapshot is not None else None
        self.store = ConversationStore(paths.conversations)
        self.environment = EnvironmentService(paths.environment)
        self.drafts = DraftStore(paths.drafts)
        self.turn_options = TurnOptionsStore(paths.turn_options)
        self.attachments = ImageStore(paths.attachments)
        self.request_records = RequestRecordStore(paths.request_records)
        self.prompt_presets = PromptPresets(paths.prompt_presets)
        self.general_settings = GeneralSettings(paths.general)
        self._human_interaction = human_interaction
        from .skills import SkillService
        self.skills = SkillService(paths.skills, enabled_plugins=enabled_plugins, roots=skill_roots,
                                   manual_invocation=skill_manual_invocation, plugin_catalog=self.plugin_catalog)
        self._mutation_lock = mutation_lock
        self._delegated_records = frozenset(delegated_records)
        self._delegated_calls = frozenset(delegated_calls)
        self._excluded_env = frozenset(excluded_env)
        self._enabled_plugins = enabled_plugins
        from .subagents import SubagentManager
        self.subagents = SubagentManager(directory, tasks) if enable_subagents else None

    def conversation_ids(self) -> tuple[str, ...]:
        """只列已有 UUID 文件，不为显示列表反复读取整段历史。"""
        values = []
        for path in self.store.directory.glob("*.jsonl"):
            try:
                values.append(UUID(path.stem).hex)
            except ValueError:
                continue
        return tuple(sorted(values))

    async def summarize_conversations(self, known: dict[str, tuple[int, int]] | None = None) -> ConversationSummaryScan:
        """只扫描新／变化日志；一个日志不可读不阻断其他会话。"""
        summaries, issues = [], []
        for conversation_id in self.conversation_ids():
            # 在日志之间响应取消；当前同步扫描完成并释放文件锁后才能退出。
            await asyncio.sleep(0)
            try:
                stamp = self.store.path(conversation_id).stat()
                if known is not None and known.get(conversation_id) == (stamp.st_size, stamp.st_mtime_ns):
                    continue
                summaries.append(self.store.summarize(conversation_id))
            except (OSError, ValueError) as exc:
                issues.append(f"会话 {conversation_id}：{exc}")
        return ConversationSummaryScan(tuple(summaries), tuple(issues))

    def open(self, conversation_id: str) -> ConversationUpdate:
        """只读打开，不恢复执行、不发模型请求，也不从日志自动授权工具。"""
        conversation = self.store.load(conversation_id)
        conversation_id = conversation.conversation_id
        draft_error = options_error = None
        from .drafts import InputDraft
        try:
            draft = self.drafts.load_input(conversation_id)
            self.drafts.protected.discard(conversation_id)
            navigation = conversation.navigation
            if navigation and navigation.get("draft") is not None and draft.navigation_id != navigation["operation_id"]:
                from ..llm.messages import content_from_dict
                restored = navigation["draft"]
                draft = InputDraft(restored["text"], [content_from_dict(part) for part in restored["images"]],
                                   navigation_id=navigation["operation_id"])
                self.drafts.save(conversation_id, draft.text, draft.images, navigation_id=draft.navigation_id)
            if draft.submitted_input_id is not None:
                saved = next((item.message for item in conversation.inputs if item.input_id == draft.submitted_input_id), None)
                if saved is None:
                    saved = next((turn.user for turn in conversation.turns if turn.run_id == draft.submitted_input_id), None)
                if saved is not None and saved.text == draft.text and [part for part in saved.content if isinstance(part, ImageContent)] == draft.images:
                    # 原文确实落盘才隐去已提交草稿；旧文件保留，下一次编辑按原原子保存路线写入。
                    draft = InputDraft()
            self.drafts.protected.discard(conversation_id)
        except (OSError, ValueError) as exc:
            self.drafts.protected.add(conversation_id)
            draft, draft_error = InputDraft(), f"草稿读取失败，原文件受保护：{exc}"
        try:
            options = self.turn_options.load(conversation_id, navigation=conversation.navigation)
            options = options if options is not None else conversation.last_options
            if options.model_target is None:
                options = replace(options, model_target=conversation.last_options.model_target)
            # 下一轮默认总沿用户主动应用的配置；旧队列快照另在 input_submitted 中。
            options = replace(options, prompt_config=conversation.current_prompt)
            options.validate(conversation.settings)
            self.turn_options.protected.discard(conversation_id)
        except (OSError, ValueError) as exc:
            self.turn_options.protected.add(conversation_id)
            options, options_error = None, f"下一轮选项读取失败，原文件受保护：{exc}"
        record_run_id, record_warning = self.request_records.latest(conversation_id)
        return ConversationUpdate(conversation, draft=draft.text, draft_images=tuple(draft.images),
                                  turn_options=options, draft_error=draft_error, options_error=options_error,
                                  record_run_id=record_run_id, record_warning=record_warning)

    def rewind(self, conversation_id: str, anchor, *, operation_id: str, draft: dict,
               options: TurnOptions, replacement: str | None = None) -> ConversationUpdate:
        """修订先持久保存；草稿失败可按同一操作编号补存，不再裁一次。"""
        from ..runtime.conversations.revisions import selected_message, revision_unavailable
        with self.store.writer(conversation_id) as journal:
            conversation = journal.conversation
            if not conversation.navigation or conversation.navigation["operation_id"] != operation_id:
                message = selected_message(conversation, anchor)
                reason = revision_unavailable(conversation)
                if reason:
                    raise ValueError(reason)
                if self.subagents is not None:
                    self.subagents.stop_parent(conversation_id)
                if conversation.active_turn is not None or conversation.active_compaction is not None:
                    # 重开后的遗留运行没有活线程；沿已有结算路线标明未知结果，不执行工具。
                    self._service(options.model_target.config if options.model_target else conversation.settings.config,
                                  conversation.settings.tool_names, network=False)._recover(journal)
                restored = None if replacement is not None else {
                    "text": message.text, "images": [asdict(part) for part in message.content if isinstance(part, ImageContent)]}
                journal.append("history_revised", operation_id, {"anchor": asdict(anchor), "replacement": replacement,
                    "draft": restored, "displaced_draft": draft, "options": asdict(options)})
        self.save_turn_options(conversation, options)
        return self.open(conversation_id)

    def fork(self, conversation_id: str, anchor, *, new_id: str, operation_id: str,
             options: TurnOptions, title: str) -> ConversationUpdate:
        """新文件仅含有效前缀；不改变原运行，也不搬动项目文件。"""
        from ..runtime.conversations.revisions import selected_message, cut_branch_history, seed_payload
        from ..runtime.session import utc_now
        if self.store.path(new_id).exists():
            existing = self.store.load(new_id)
            if not existing.navigation or existing.navigation.get("operation_id") != operation_id:
                raise ValueError("分支目标已经存在，禁止覆盖。")
            self.save_turn_options(existing, TurnOptions(**existing.navigation["options"]))
            return self.open(new_id)
        source, point_time = self.store.load_branch_point(conversation_id, anchor)
        message = selected_message(source, anchor)
        options = cut_branch_history(source, anchor, point_time=point_time, timestamp=utc_now())
        options.validate(source.settings)
        source.navigation = {"operation_id": operation_id, "source_conversation_id": conversation_id, "branch_title": title,
            "time": utc_now(),
            "options": asdict(options),
            "anchor": asdict(anchor), "draft": {"text": message.text,
            "images": [asdict(part) for part in message.content if isinstance(part, ImageContent)]}}
        # 原头部模型仍用于没有逐轮目标的旧记录归属；新轮目标另存 options。
        settings = deepcopy(source.settings)
        self.store.create(settings, conversation_id=new_id, seed=seed_payload(source))
        self.save_turn_options(self.store.load(new_id), options)
        return self.open(new_id)

    def apply_prompt(self, conversation_id: str, prompt_config, *, preset_id: str | None = None,
                     preset_name: str | None = None) -> ConversationUpdate:
        with self.store.writer(conversation_id) as journal:
            # 预对话是创建前单独编辑的内容，切换规则预设不能替换它。
            prompt_config = replace(prompt_config, prelude=deepcopy(journal.conversation.settings.prompt_config.prelude))
            journal.append("prompt_applied", uuid4().hex, {"prompt_config": asdict(prompt_config),
                           "preset_id": preset_id, "preset_name": preset_name})
        conversation = journal.conversation
        options = replace(self.turn_options.load(conversation_id) or conversation.last_options, prompt_config=prompt_config)
        self.save_turn_options(conversation, options)
        return self.open(conversation_id)

    def request_record_directory(self, conversation_id: str, run_id: str) -> Path:
        """用户打开目录前核对归属，只读小清单，不读取请求正文。"""
        return self.request_records.directory_for(conversation_id, run_id)

    def save_turn_options(self, conversation: Conversation, options: TurnOptions) -> None:
        """校验所选目标及原授权；不联网、不改旧轮次，也不争用历史写锁。"""
        options.validate(conversation.settings)
        self.turn_options.save(conversation.conversation_id, options,
                               navigation_id=conversation.navigation["operation_id"] if conversation.navigation else None)

    def estimate_context(self, conversation: Conversation, environment: EnvironmentCheck | None = None) -> int:
        """兼容的只读请求粗估接口；当前上下文圆环不再使用这份估计。

        环境采用已有快照，真正发送仍重新检测；结果不是厂商 tokenizer。
        自动压缩在Loop准备实际请求后测量，不能以本接口的空草稿替代。
        """
        settings = conversation.settings
        messages = [ModelMessage("system", settings.system)] if settings.system else []
        options = conversation.last_options
        config = options.model_target.config
        messages.extend(build_history(conversation, config=config))
        messages.append(ModelMessage("user", [TextContent("")]))
        runtime = create_runtime(config, "", attachments=self.attachments, plugin_catalog=self.plugin_catalog,
                                 enabled_plugins=self.enabled_plugin_names())
        prepared, _ = runtime.prepare_agent_request(
            ModelRequest(messages, settings.temperature, settings.max_output_tokens,
                         reasoning_effort=options.reasoning_effort),
            workspace=settings.workspace, tool_names=settings.tool_names,
            prompt_config=options.prompt_config or conversation.current_prompt, prompt_mode=settings.prompt_mode,
            shell_config=environment.shell if environment is not None else settings.shell_config,
            search_config=environment.search if environment is not None else settings.search_config,
            program_paths=environment.program_paths if environment is not None else settings.program_paths,
            environment_checked=True,
            context_policy=replace(settings.context_policy, context_window=effective_window(config.context_window, settings.context_policy.context_window)),
            automatic_compaction=True,
            runtime_baseline=conversation.runtime_baseline,
        )
        return estimate_request_tokens(prepared)

    def check_environment(
        self, settings: ConversationSettings, *, required_programs: Sequence[str] = (),
    ) -> EnvironmentCheck:
        if isinstance(required_programs, str) or set(required_programs) - {"python", "git"}:
            raise ValueError("附加任务环境须为 python／git 名称序列。")
        if settings.prompt_mode == "dialogue":
            if required_programs or settings.program_paths:
                raise ValueError("纯对话不接收本地程序要求。")
            return EnvironmentCheck()
        paths = {name: None for name in required_programs}
        paths.update(settings.program_paths)
        return self.environment.check(
            shell=(settings.shell_config or ShellConfig()) if "run_command" in settings.tool_names else None,
            search=(settings.search_config or SearchConfig()) if SEARCH_TOOLS.intersection(settings.tool_names) else None,
            program_paths=paths,
        )

    def create(self, settings: ConversationSettings, *, required_programs: Sequence[str] = ()) -> ConversationUpdate:
        check = self.check_environment(settings, required_programs=required_programs)
        # 已有路径固定实际文件；用户指定但暂时缺失的路径保留选择，下轮仍检查它。
        shell, search = check.shell, check.search
        if shell is not None and shell.executable is None and settings.shell_config is not None:
            shell = replace(shell, executable=settings.shell_config.executable)
        if search is not None and search.backend == "rg" and search.executable is None and settings.search_config is not None:
            search = replace(search, executable=settings.search_config.executable)
        paths = {name: path if path is not None else settings.program_paths.get(name)
                 for name, path in check.program_paths.items()}
        prepared = replace(deepcopy(settings), config=resolve_model_config(settings.config), shell_config=shell, search_config=search,
                           program_paths=paths)
        service = self._service(prepared.config, prepared.tool_names, network=False)
        return ConversationUpdate(service.create(prepared, environment=check), check, draft="")

    def _service(self, config: ModelConfig, tool_names: Sequence[str], *, network: bool) -> ConversationService:
        """创建后端不发请求；密钥只在实际发送／摘要时按引用读取。"""
        config = resolve_model_config(config)
        key = resolve_api_key(config) if network else ""
        enabled = self.enabled_plugin_names()
        mcp_issues = ()
        try:
            servers = self._mcp_snapshot if self._mcp_snapshot is not None else self.mcp_settings.load()
        except (OSError, ValueError):
            servers = ()
            mcp_issues = ("MCP设置不可读，此运行未开放MCP；其他能力继续，原设置保留。",)
        runtime = create_runtime(config, key, attachments=self.attachments, artifacts_dir=self.paths.tool_results,
                                 enabled_plugins=enabled, skills=self.skills, excluded_env=self._excluded_env,
                                 plugin_catalog=self.plugin_catalog, mcp_servers=servers, registration_issues=mcp_issues)
        preparation = RunPreparation()
        if self._mutation_lock is not None:
            preparation = RunPreparation(bind_tools=lambda context: replace(context, mutation_lock=self._mutation_lock,
                                                           reference_scope_id=self._reference_scope_id,
                                                           artifact_ids=context.artifact_ids | set(self._delegated_records),
                                                           artifact_calls=context.artifact_calls | set(self._delegated_calls)))
        return ConversationService(runtime, self.store, config, tool_names=list(tool_names), preparation=preparation)

    def enabled_plugin_names(self):
        if self._enabled_plugins is not None:
            return self._enabled_plugins
        from .plugin_settings import PluginSettings
        try:
            values = PluginSettings(self.paths.root / "plugins.json").values()
        except (OSError, ValueError):
            # 损坏的开关不能默认为允许导入本地Python插件；设置页保留原文件并显示原因。
            return frozenset()
        return frozenset(item.plugin_id for item in self.plugin_catalog.descriptors if values.get(item.plugin_id, item.default_enabled))

    def default_tool_names(self, profile="desktop_work"):
        from ..tools.catalog import default_tool_names
        return default_tool_names(profile, self.plugin_catalog, enabled_plugins=self.enabled_plugin_names())

    def prepare_user_message(self, prompt, images=None, *, workspace=None):
        """供集成方显式提前冻结技能；普通GUI提交由后台服务冻结，避免阻塞界面。"""
        return self.skills.prepare_message(prompt, images, workspace=workspace)

    async def send_message(
        self, conversation_id: str, prompt: str, *, tool_names: Sequence[str], run_id: str,
        on_event: AgentEventSink | None = None,
        images: list[ImageContent] | None = None,
        options: TurnOptions | None = None,
        record_requests: bool = False,
        on_record: Callable[[RequestRecordNotice], None] | None = None,
        control: ConversationControl | None = None,
        regeneration_inputs: list[ModelMessage] | None = None,
        message: ModelMessage | None = None,
    ) -> ConversationUpdate:
        return await self._execute_turn(conversation_id, tool_names=tool_names, run_id=run_id, on_event=on_event,
            prompt=prompt, images=images, options=options, record_requests=record_requests, on_record=on_record, control=control,
            regeneration_inputs=regeneration_inputs, message=message)

    async def resume_message(self, conversation_id: str, expected_run_id: str, prompt: str = "", **options) -> ConversationUpdate:
        """继续是新运行；目标和强度由本次输入台快照提供，权限沿原会话。"""
        return await self._execute_turn(conversation_id, prompt=prompt, resumed_from_run_id=expected_run_id, **options)

    async def send_queued(self, conversation_id: str, input_id: str, **options) -> ConversationUpdate:
        return await self._execute_turn(conversation_id, source_input_id=input_id, **options)

    async def send_program(self, conversation_id: str, events: list[dict], **options) -> ConversationUpdate:
        """程序完成通知续接；不制造用户原文，也不消费输入台草稿。"""
        return await self._execute_turn(conversation_id, program_events=events, **options)

    async def _execute_turn(
        self, conversation_id: str, *, tool_names: Sequence[str], run_id: str,
        prompt: str = "", images: list[ImageContent] | None = None, options: TurnOptions | None = None,
        on_event: AgentEventSink | None = None, record_requests: bool = False,
        on_record: Callable[[RequestRecordNotice], None] | None = None,
        control: ConversationControl | None = None,
        source_input_id: str | None = None, resumed_from_run_id: str | None = None,
        regeneration_inputs: list[ModelMessage] | None = None,
        program_events: list[dict] | None = None,
        message: ModelMessage | None = None,
    ) -> ConversationUpdate:
        settings = self.store.load_settings(conversation_id)
        if not set(settings.tool_names).issubset(tool_names):
            raise ValueError("当前应用未开放原会话所需工具，请明确选择后再发送。")
        if source_input_id is not None:
            conversation = self.store.load(conversation_id)
            queue = conversation.pending_inputs("follow_up")
            if conversation.resumable_turn is not None or not queue or queue[0].input_id != source_input_id:
                raise ValueError("原目标未完成或排队项已变化，未发送请求。")
            options = queue[0].options
        elif options is None:
            conversation = self.store.load(conversation_id)
            options = self.turn_options.load(conversation_id, navigation=conversation.navigation) or conversation.last_options
            options = replace(options, prompt_config=conversation.current_prompt)
        if options.prompt_config is None:
            options = replace(options, prompt_config=self.store.load(conversation_id).current_prompt)
        target = options.model_target or settings.model_target
        target = replace(target, config=resolve_model_config(target.config))
        options = replace(options, model_target=target)
        options.validate(settings)
        check = self.check_environment(settings)
        service = self._service(target.config, tool_names, network=True)
        self._bind_subagents(service, conversation_id, run_id, settings,
                             purpose="program" if program_events is not None else "resume" if resumed_from_run_id else "new")
        self._bind_questions(service)
        # 流式开关由 on_text 决定；文字已包含在 AgentEvent，不再重复通知界面。
        result = None
        try:
            async with self.request_records.recording(record_requests, RequestTraceContext(run_id, conversation_id),
                api_style=target.config.api_style, model=target.config.model,
                prompt_source={"origin": "turn_snapshot", "version": options.prompt_config.version,
                               "source": options.prompt_config.source, "mode": settings.prompt_mode},
                on_notice=on_record) as traces:
                arguments = dict(run_id=run_id, on_text=lambda _: None, on_event=on_event, environment=check,
                                 options=options, request_traces=traces, control=control)
                if program_events is not None:
                    result = await service.send_program(conversation_id, program_events, **arguments)
                elif resumed_from_run_id is not None:
                    result = await service.resume_message(conversation_id, resumed_from_run_id, prompt, images=images, message=message, **arguments)
                elif source_input_id is not None:
                    result = await service.send_queued(conversation_id, source_input_id, **arguments)
                else:
                    result = await service.send_message(conversation_id, prompt, images=images,
                                                        regeneration_inputs=regeneration_inputs, message=message, **arguments)
                if traces is not None:
                    traces.set_outcome(result.stop_reason)
        except AgentCancelled as exc:
            if self.subagents is not None:
                self.subagents.stop_parent(conversation_id)
            raise ConversationCancelled(self._reload(conversation_id, check, exc.result)) from exc
        except asyncio.CancelledError as exc:
            if self.subagents is not None:
                self.subagents.stop_parent(conversation_id)
            # 诊断开始／结算期间也可能取消；读回原事实，不重新发消息。
            raise ConversationCancelled(self._reload(conversation_id, check, result)) from exc
        if self.subagents is not None and program_events is not None and result is not None:
            consumed = {identifier for step in result.steps for identifier in step.program_event_ids}
            self.subagents.acknowledge(conversation_id, consumed, wake=True)
        return self._reload(conversation_id, check, result)

    def _bind_questions(self, service):
        """主界面提供提问能力；频率在独立运行开始时冻结，工具声明保持静态。"""
        if not self._human_interaction:
            return
        from ..tools.questions import QUESTION_TOOL
        frequency = self.general_settings.load().question_frequency
        service.prepare_run(RunPreparation(additional_tool_names=(QUESTION_TOOL,),
            bind_tools=lambda context: replace(context, question_frequency=frequency)))

    def _bind_subagents(self, service, conversation_id, run_id, settings, *, purpose):
        """普通运行与手动摘要共用能力准备；摘要不改变目标或消费完成通知。"""
        if self.subagents is None or settings.prompt_mode != "work":
            return
        from ..runtime.subagents import SUBAGENT_TOOLS
        enabled = service.enabled_plugins
        def bind(context):
            access, lock, available = self.subagents.bind(conversation_id, run_id, settings, context,
                new_target=purpose == "new", human_resume=purpose == "resume")
            selected = context.allowed_tools if available else context.allowed_tools - SUBAGENT_TOOLS
            if not access.profiles:
                selected = selected - {"subagent"}
            result_context = replace(context, allowed_tools=selected, mutation_lock=lock,
                                     subagents=access if available else None)
            access.context = result_context
            access.enabled_plugins = enabled
            access.plugin_snapshot = self.plugin_catalog.snapshot()
            from ..mcp.settings import tool_name
            access.mcp_snapshot = tuple(server for server in service.mcp_snapshot if server.enabled
                and any(tool_name(server.server_id, tool["name"]) in selected for tool in server.tools))
            return result_context
        service.prepare_run(RunPreparation(
            additional_tool_names=tuple(sorted(SUBAGENT_TOOLS)), bind_tools=bind,
            program_source=(lambda: self.subagents.notices(conversation_id)) if purpose != "summary" else None,
            program_ack=(lambda ids: self.subagents.acknowledge(conversation_id, ids)) if purpose != "summary" else None,
        ))

    async def compact(self, conversation_id: str, *, tool_names: Sequence[str], on_event: AgentEventSink | None = None,
                      run_id: str | None = None, record_requests: bool = False,
                      on_record: Callable[[RequestRecordNotice], None] | None = None,
                      options: TurnOptions | None = None) -> ConversationUpdate:
        settings = self.store.load_settings(conversation_id)
        if options is None:
            conversation = self.store.load(conversation_id)
            options = self.turn_options.load(conversation_id, navigation=conversation.navigation) or conversation.last_options
            options = replace(options, prompt_config=conversation.current_prompt)
        elif options.prompt_config is None:
            options = replace(options, prompt_config=self.store.load(conversation_id).current_prompt)
        target = options.model_target or settings.model_target
        target = replace(target, config=resolve_model_config(target.config))
        options = replace(options, model_target=target)
        options.validate(settings)
        check = self.check_environment(settings)
        service = self._service(target.config, tool_names, network=True)
        run_id = run_id or uuid4().hex
        self._bind_subagents(service, conversation_id, run_id, settings, purpose="summary")
        self._bind_questions(service)
        try:
            async with self.request_records.recording(record_requests,
                RequestTraceContext(run_id, conversation_id, purpose="compaction"),
                api_style=target.config.api_style, model=target.config.model,
                prompt_source={"origin": "turn_snapshot", "version": options.prompt_config.version,
                               "source": options.prompt_config.source, "mode": settings.prompt_mode},
                on_notice=on_record) as traces:
                result = await service.compact(conversation_id, on_event=on_event, environment=check, request_traces=traces, options=options)
                if traces is not None:
                    traces.set_outcome(result.status)
        except asyncio.CancelledError as exc:
            raise ConversationCancelled(self._reload(conversation_id, check)) from exc
        return self._reload(conversation_id, check, result)

    def _reload(self, conversation_id: str, check: EnvironmentCheck | None = None,
                result: AgentResult | CompactionRecord | None = None) -> ConversationUpdate:
        try:
            return ConversationUpdate(self.store.load(conversation_id), check, result)
        except (OSError, ValueError) as exc:
            raise ConversationViewError(conversation_id, result) from exc

    def retry_save(
        self, error: ConversationSaveError | CompactionSaveError, config: ModelConfig, *, tool_names: Sequence[str],
    ) -> ConversationUpdate:
        """保存失败只能补存原事实；不重复发送消息、摘要或工具。"""
        service = self._service(config, tool_names, network=False)
        result = service.retry_save(error) if isinstance(error, ConversationSaveError) else service.retry_compaction_save(error)
        return self._reload(error.conversation_id, result=result)
