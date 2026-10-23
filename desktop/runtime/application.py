"""供命令行和桌面窗口共用的平台入口。"""
import asyncio
import hashlib
import json
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from copy import deepcopy
from dataclasses import asdict, replace
from functools import partial
from pathlib import Path
from typing import Any

from ..llm.contracts import ModelBackend, ModelSessionFactory, RequestSink, TextSink
from ..llm.diagnostics import RequestTraceFactory, call_observed_model
from ..llm.models import ModelRequest, ModelResponse
from ..llm.messages import ModelMessage, ToolResult, expand_skill_messages
from ..llm.attempts import ModelAttemptSink
from ..llm.validation import parse_max_output_tokens, parse_temperature
from ..storage.paths import LocalPaths
from ..prompts import PromptConfig, assemble_agent_messages, load_prompt_config
from ..tools.contracts import ToolContext
from ..tools.registry import ToolRegistry
from ..tools.records import issued_record_ids
from ..tools.command.shells import ShellConfig, resolve_shell
from ..tools.filesystem.search_config import SEARCH_TOOLS, SearchConfig, resolve_search_config
from ..tools.filesystem.discovery import SKIP_DIRECTORIES
from ..tools.filesystem.limits import MAX_DIRECTORIES, MAX_EXAMINED_ENTRIES, MAX_SEARCH_READ_FILES, MAX_SEARCH_READ_BYTES, MAX_RAW_BYTES
from .agent.loop import AgentLoop
from .agent.models import AgentInputSource
from .agent.models import AgentCancelled, AgentCheckpointSink, AgentEventSink, AgentLimits, AgentRequestPolicy, AgentResult
from .context.budget import ContextPolicy
from .contracts import ExecutionContext, RunPreparation
from .registry import PluginRegistry
from .runs import exception_diagnostics


class Runtime:
    """保存已组装的能力和注册表，将业务输入交给对应插件。"""

    def __init__(
        self, registry: PluginRegistry, context: ExecutionContext, *,
        tools: ToolRegistry | None = None, model_session: ModelSessionFactory | None = None,
        artifacts_dir: Path | None = None, excluded_env: frozenset[str] = frozenset(),
        context_window: int | None = None,
        enabled_plugins: frozenset[str] | None = None,
        skills=None,
        mcp_servers=(),
        extension_plugin_ids=frozenset(),
    ) -> None:
        self._registry = registry
        self._context = context
        self._tools = tools if tools is not None else ToolRegistry()
        self._model_session = model_session
        directory = artifacts_dir if artifacts_dir is not None else LocalPaths.default().tool_results
        self._artifacts_dir = directory.expanduser().resolve()
        self._excluded_env = excluded_env
        self._context_window = context_window
        self._enabled_plugins = enabled_plugins
        self.mcp_servers = tuple(mcp_servers)
        self._extension_plugin_ids = extension_plugin_ids
        if skills is None:
            from ..application.skills import SkillService
            skills = SkillService(LocalPaths.default().skills, enabled_plugins=enabled_plugins)
        self.skills = skills

    @property
    def enabled_plugins(self) -> frozenset[str] | None:
        """提供只读开关快照，子任务继承时不访问执行器私有状态。"""
        return self._enabled_plugins

    def model_session(self) -> AbstractAsyncContextManager[ModelBackend]:
        """摘要与 Agent 共用连接创建约定；调用方用 async with 确保取消时关闭。"""
        if self._model_session is None:
            raise ValueError("尚未配置 Agent 模型调用。")
        return self._model_session()

    def prepare_agent_request(
        self, request: ModelRequest, *, workspace: Path | str | None, tool_names: Sequence[str],
        prompt_config: PromptConfig | None = None,
        prompt_mode: str = "work",
        shell_config: ShellConfig | None = None,
        search_config: SearchConfig | None = None,
        program_paths: dict[str, str | None] | None = None,
        environment_checked: bool = False,
        context_policy: ContextPolicy | None = None,
        automatic_compaction: bool = False,
        runtime_baseline: ModelMessage | None = None,
        preparation: RunPreparation | None = None,
    ) -> tuple[ModelRequest, ToolContext | None]:
        """准备规则、环境和实际声明；手动预算与执行请求采用同一个装配入口。"""
        if request.tools:
            raise ValueError("Agent 的工具声明来自本地登记，请通过 tool_names 选择，而非 request.tools。")
        if isinstance(tool_names, str):
            raise ValueError("tool_names 应为工具名称序列，不能是单个字符串。")
        max_output_tokens = parse_max_output_tokens(request.max_output_tokens)
        preparation = preparation if preparation is not None else RunPreparation()
        registered_names = frozenset(tool.schema.name for tool in self._tools.definitions())
        optional_names = tuple(name for name in preparation.additional_tool_names if name in registered_names)
        root, context, schemas = None, None, []
        shell, search = None, None
        scope = self.skills.scope(workspace, refresh=True, enabled_plugins=self._enabled_plugins)
        scope.restore(request.messages)
        from ..skills.tools import SKILL_TOOLS
        from ..tools.questions import QUESTION_TOOL, QUESTION_POLICIES
        skill_names = SKILL_TOOLS if scope.entries else ()
        if prompt_mode == "dialogue":
            if workspace is not None or tool_names or shell_config is not None or search_config is not None or program_paths:
                raise ValueError("纯对话模式不接收工作目录或本地工具。")
            dialogue_names = (*skill_names, *(name for name in optional_names if name == QUESTION_TOOL))
            schemas = sorted(self._tools.schemas(dialogue_names), key=lambda tool: tool.name)
            if schemas:
                context = ToolContext(Path.home(), frozenset(dialogue_names), artifacts_dir=self._artifacts_dir, skills=scope)
                if preparation.bind_tools is not None:
                    context = preparation.bind_tools(context)
                    schemas = [tool for tool in schemas if tool.name in context.allowed_tools]
        elif prompt_mode == "work":
            if workspace is None:
                raise ValueError("工作模式需要已有工作目录。")
            extension_names = tuple(item.schema.name for item in self._tools.definitions()
                if "desktop_work" in item.default_profiles and (
                    (self._tools.source(item.schema.name) or "").startswith("mcp:")
                    or self._tools.source(item.schema.name) in self._extension_plugin_ids))
            selected_tools = list(dict.fromkeys((*tool_names, *optional_names, *skill_names, *extension_names,
                                                *(("read_tool_record",) if extension_names else ()))))
            schemas = sorted(self._tools.schemas(selected_tools), key=lambda tool: tool.name)
            if self._enabled_plugins is not None:
                schemas = [tool for tool in schemas if self._tools.source(tool.name) is None
                           or self._tools.source(tool.name) in self._enabled_plugins
                           or (self._tools.source(tool.name) or "").startswith("mcp:")
                           or tool.name in preparation.additional_tool_names]
            root = Path(workspace).expanduser().resolve(strict=True)
            if not root.is_dir():
                raise ValueError("Agent 工作目录必须是已有目录。")
            names = frozenset(tool.name for tool in schemas)
            if "run_command" in names:
                selected = shell_config if shell_config is not None else ShellConfig()
                shell = selected if environment_checked else resolve_shell(selected)
            elif shell_config is not None:
                raise ValueError("Shell 配置需要同时开放 run_command。")
            if names & SEARCH_TOOLS:
                selected = search_config if search_config is not None else SearchConfig()
                search = selected if environment_checked else resolve_search_config(selected)
            elif search_config is not None:
                raise ValueError("搜索配置需要开放 find_files 或 search_text。")
            if search is not None and search.backend == "rg" and (
                    search.executable is None or search.probe is not None and not search.probe.get("available")):
                schemas = [tool for tool in schemas if tool.name not in SEARCH_TOOLS]
                names = frozenset(tool.name for tool in schemas)
            context = ToolContext(root, names, shell=shell, artifacts_dir=self._artifacts_dir,
                                  excluded_env=self._excluded_env, search_config=search, skills=scope,
                                  artifact_calls={message.call_id for message in request.messages if isinstance(message, ToolResult)},
                                  artifact_ids=issued_record_ids((message for message in request.messages if isinstance(message, ToolResult)),
                                      self._artifacts_dir) if "read_tool_record" in names else set())
            if preparation.bind_tools is not None:
                context = preparation.bind_tools(context)
                schemas = [tool for tool in schemas if tool.name in context.allowed_tools]
        else:
            raise ValueError("提示词模式须为 dialogue 或 work。")
        file_search = asdict(search) if search is not None else None
        if search is not None:
            file_search["ignore_directories"] = sorted(SKIP_DIRECTORIES)
            file_search["encoding_validation"] = "scanned_utf8_chunks" if search.backend == "python" else "returned_rg_lines_and_binary_report"
            file_search["budgets"] = ({"directories": MAX_DIRECTORIES, "entries": MAX_EXAMINED_ENTRIES,
                "read_files": MAX_SEARCH_READ_FILES, "read_bytes": MAX_SEARCH_READ_BYTES,
                "search_file_bytes": None} if search.backend == "python" else
                {"raw_output_bytes": MAX_RAW_BYTES, "search_file_bytes": None, "timeout_seconds": search.timeout_seconds})
            file_search["available"] = search.backend == "python" or (search.executable is not None and (search.probe is None or search.probe.get("available") is True))
            if not file_search["available"]:
                file_search["unavailable_tools"] = sorted(SEARCH_TOOLS.intersection(tool_names))
        messages = assemble_agent_messages(
            expand_skill_messages(request.messages), prompt_config if prompt_config is not None else load_prompt_config(), root, schemas,
            prompt_mode=prompt_mode,
            command_shell={"kind": shell.kind, "executable": shell.executable, "timeout_seconds": shell.timeout_seconds} if shell is not None else None,
            file_search=file_search,
            program_paths=program_paths,
            context_info={
                "tool_schema_sha256": hashlib.sha256(json.dumps([asdict(tool) for tool in schemas],
                    ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
                "tool_registration_issues": self._tools.registration_errors[:4],
                **({"unavailable_optional_tools": sorted(set(preparation.additional_tool_names) - registered_names)}
                   if set(preparation.additional_tool_names) - registered_names else {}),
                "available_skills": scope.model_catalog() if context is not None and "load_skill" in context.allowed_tools else [],
                "skill_catalog_issues": scope.issues[:8],
                "skill_catalog_issue_count": len(scope.issues),
                "skill_catalog_complete": not bool(scope.issues),
                "subagent_profiles": ([{"profile_id": key, "name": value["profile"]["name"],
                    "description": value["profile"]["description"]} for key, value in context.subagents.profiles.items()]
                    if context is not None and context.subagents is not None else []),
                **({"question_policy": {"frequency": context.question_frequency,
                    "guidance": QUESTION_POLICIES[context.question_frequency], "blocking": False}}
                   if context is not None and context.question_frequency in QUESTION_POLICIES else {}),
                "context_window": context_policy.context_window if context_policy is not None else self._context_window,
                "context_window_source": "resolved_context_policy" if context_policy is not None else "model_config",
                "compression": {
                    "automatic": automatic_compaction and context_policy is not None and context_policy.context_window is not None,
                    "trigger_ratio": context_policy.trigger_ratio if context_policy is not None else None,
                    "input_threshold_tokens": context_policy.threshold(max_output_tokens) if context_policy is not None else None,
                    "usage_basis": "local_request_estimate; not_provider_usage",
                },
            },
            runtime_baseline=runtime_baseline,
        )
        return replace(
            request, messages=messages, tools=schemas,
            temperature=parse_temperature(request.temperature),
            max_output_tokens=max_output_tokens,
            runtime_context_id=next(message.runtime_context_id for message in messages
                                    if isinstance(message, ModelMessage) and message.runtime_context_kind == "baseline"),
            runtime_context_updates=([messages[-1]] if isinstance(messages[-1], ModelMessage)
                and messages[-1].runtime_context_kind == "environment_update"
                and not any(messages[-1] is original for original in request.messages) else []),
        ), context

    async def run_agent(
        self, request: ModelRequest, *, workspace: Path | str | None, tool_names: Sequence[str],
        limits: AgentLimits | None = None, on_text: TextSink | None = None,
        on_event: AgentEventSink | None = None,
        prompt_config: PromptConfig | None = None,
        on_checkpoint: AgentCheckpointSink | None = None,
        before_request: AgentRequestPolicy | None = None,
        request_traces: RequestTraceFactory | None = None,
        prompt_mode: str = "work",
        shell_config: ShellConfig | None = None,
        search_config: SearchConfig | None = None,
        program_paths: dict[str, str | None] | None = None,
        environment_checked: bool = False,
        context_policy: ContextPolicy | None = None,
        preparation: RunPreparation | None = None,
    ) -> AgentResult:
        """应用确定工具与目录；循环使用同一次运行内的模型连接。"""
        if self._model_session is None:
            raise ValueError("尚未配置 Agent 模型调用。")
        prepared, context = self.prepare_agent_request(
            request, workspace=workspace, tool_names=tool_names, prompt_config=prompt_config, prompt_mode=prompt_mode,
            shell_config=shell_config,
            search_config=search_config,
            program_paths=program_paths,
            environment_checked=environment_checked,
            context_policy=context_policy,
            automatic_compaction=before_request is not None,
            preparation=preparation,
        )
        return await self.run_prepared_agent(prepared, context, limits=limits, on_text=on_text,
            on_event=on_event, on_checkpoint=on_checkpoint, before_request=before_request, request_traces=request_traces)

    async def run_prepared_agent(
        self, prepared: ModelRequest, context: ToolContext | None, *, limits: AgentLimits | None = None,
        on_text: TextSink | None = None, on_event: AgentEventSink | None = None,
        on_checkpoint: AgentCheckpointSink | None = None, before_request: AgentRequestPolicy | None = None,
        request_traces: RequestTraceFactory | None = None,
        input_source: AgentInputSource | None = None,
    ) -> AgentResult:
        """会话先保存装配资料再进入连接；CLI仍通过run_agent准备后复用此生命周期。"""
        if self._model_session is None:
            raise ValueError("尚未配置 Agent 模型调用。")
        result = None
        try:
            from ..mcp.pool import mcp_pool
            async with mcp_pool(self.mcp_servers) as external, self.model_session() as backend:
                if context is not None:
                    context = replace(context, mcp=external)
                try:
                    result = await AgentLoop(self._tools).run(
                        prepared, backend, context, limits if limits is not None else AgentLimits(),
                        on_text=on_text, on_event=on_event, on_checkpoint=on_checkpoint,
                        before_request=before_request,
                        request_traces=request_traces,
                        input_source=input_source,
                    )
                except AgentCancelled as exc:
                    result = exc.result
                    raise
            return result
        except AgentCancelled:
            raise
        except asyncio.CancelledError as exc:
            # 连接打开或关闭期间的取消也保留事实，不强行返回成功。
            result = result if result is not None else AgentResult(deepcopy(prepared))
            result.stop_reason = "cancelled"
            raise AgentCancelled(result) from exc
        except Exception as exc:
            if result is None:
                raise
            # 循环已结束后连接关闭失败，也不能丢掉工具执行事实。
            cancelled = result.stop_reason == "cancelled"
            result.stop_reason = "cancelled" if cancelled else "failed"
            detail = f"模型连接关闭失败（{type(exc).__name__}）。"
            result.error = f"{result.error}；{detail}" if result.error else detail
            location = exception_diagnostics(exc)
            result.diagnostics = f"{result.diagnostics}\n{location}" if result.diagnostics else location
            if cancelled:
                raise AgentCancelled(result) from exc
            return result

    async def run_action(
        self,
        plugin_id: str,
        action: str,
        payload: dict[str, Any],
        *,
        on_text: TextSink | None = None,
        on_request: RequestSink | None = None,
        on_attempt: ModelAttemptSink | None = None,
        request_traces: RequestTraceFactory | None = None,
        cancel_event=None,
    ) -> Any:
        """为本次动作选择调用方式和请求通知，不改变插件接口及共享上下文。"""
        context = replace(self._context, cancel_event=cancel_event)
        if self._enabled_plugins is not None and plugin_id not in self._enabled_plugins:
            raise ValueError("此插件已关闭，请在插件管理页启用后再执行。")
        model_call = context.complete
        if on_text is not None:
            model_call = partial(context.stream_complete, on_text=on_text)
        if on_request is not None or on_attempt is not None or request_traces is not None:
            async def observed_complete(request: ModelRequest) -> ModelResponse:
                # 插件可能继续修改自己的对象，通知中使用独立的消息快照。
                if on_request is not None:
                    on_request(deepcopy(request))
                if (on_attempt is not None or request_traces is not None) and self._model_session is not None:
                    # 只有插件真正调用模型才打开连接，不影响纯本地插件动作。
                    async with self.model_session() as backend:
                        trace = request_traces.for_request(request, purpose="plugin", plugin_id=plugin_id,
                            action=action) if request_traces else None
                        return await call_observed_model(backend, request, on_text=on_text, on_attempt=on_attempt, trace=trace)
                if request_traces is not None:
                    trace = request_traces.for_request(request, purpose="plugin", plugin_id=plugin_id, action=action)
                    await trace.unavailable()
                    try:
                        response = await model_call(request)
                    except BaseException as exc:
                        await trace.finish("cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                                           error_type=type(exc).__name__)
                        raise
                    await trace.finish("response_received", completion_status=response.completion_status)
                    return response
                return await model_call(request)

            context = replace(context, complete=observed_complete)
        elif on_text is not None:
            context = replace(context, complete=model_call)
        return await self._registry.run_action(plugin_id, action, payload, context)
