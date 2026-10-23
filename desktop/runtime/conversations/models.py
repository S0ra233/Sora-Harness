"""会话配置和轮次事实；不调用模型，也不把提示词组装结果当作原始历史。"""
from dataclasses import dataclass, field
from pathlib import Path
from math import isfinite
from uuid import UUID

from ...llm.messages import ModelMessage
from ...llm.models import ModelConfig
from ...llm.model_catalog import ModelTarget
from ...llm.reasoning import parse_effort, resolve_effort
from ...llm.validation import parse_max_output_tokens, parse_temperature, validate_model_config
from ...prompts import PromptConfig
from ..agent.models import AgentLimits, AgentStep, CompactionStatus
from ..context import ContextPolicy
from ...llm.usage import TokenUsage
from ...llm.attempts import ModelAttemptEvent
from ...tools.command.shells import ShellConfig
from ...tools.filesystem.search_config import SEARCH_TOOLS, SearchConfig


def validate_preset_identity(identifier: str | None, name: str | None) -> None:
    """预设身份只用于显示；旧记录没有身份时仍采用原配置快照。"""
    if identifier is None and name is None:
        return
    if not isinstance(identifier, str) or UUID(identifier).hex != identifier or not isinstance(name, str) or not name.strip():
        raise ValueError("预设需要有效编号和非空名称。")


@dataclass(frozen=True)
class TurnOptions:
    """下一独立轮次的生成选项；对象缺省与明确关闭强度设置分开。"""

    reasoning_effort: str | None = None
    model_target: ModelTarget | None = None
    prompt_config: PromptConfig | None = None

    def __post_init__(self) -> None:
        parse_effort(self.reasoning_effort)
        if isinstance(self.prompt_config, dict):
            object.__setattr__(self, "prompt_config", PromptConfig(**self.prompt_config))
        if self.prompt_config is not None and not isinstance(self.prompt_config, PromptConfig):
            raise ValueError("提示词选项需要完整配置快照。")
        if isinstance(self.model_target, dict):
            try:
                object.__setattr__(self, "model_target", ModelTarget(**self.model_target))
            except TypeError as exc:
                raise ValueError("轮次模型快照字段不正确。") from exc
        if self.model_target is not None and not isinstance(self.model_target, ModelTarget):
            raise ValueError("轮次目标须为模型连接快照。")

    def validate(self, settings: "ConversationSettings") -> None:
        config = self.model_target.config if self.model_target is not None else settings.config
        resolve_effort(config.reasoning, self.reasoning_effort, has_tools=bool(settings.tool_names),
                       temperature=settings.temperature, max_output_tokens=settings.max_output_tokens)


@dataclass
class ConversationSettings:
    """创建时保存配置快照；密钥仍由应用取得，工具权限由应用重新核对。"""

    config: ModelConfig
    workspace: str | None
    tool_names: list[str]
    prompt_config: PromptConfig
    system: str = ""
    temperature: float | None = None
    max_output_tokens: int | None = None
    limits: AgentLimits = field(default_factory=AgentLimits)
    context_policy: ContextPolicy = field(default_factory=ContextPolicy)
    prompt_mode: str = "work"
    shell_config: ShellConfig | None = None
    search_config: SearchConfig | None = None
    program_paths: dict[str, str | None] = field(default_factory=dict)
    reasoning_effort: str | None = None
    model_id: str | None = None
    connection_id: str | None = None
    model_label: str | None = None
    connection_label: str | None = None
    prompt_preset_id: str | None = None
    prompt_preset_name: str | None = None

    def __post_init__(self) -> None:
        validate_model_config(self.config)
        validate_preset_identity(self.prompt_preset_id, self.prompt_preset_name)
        for value in (self.model_id, self.connection_id, self.model_label, self.connection_label):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError("会话模型来源ID和显示名须为非空文本或空值。")
        if self.prompt_mode == "dialogue":
            if self.workspace is not None or self.tool_names or self.program_paths:
                raise ValueError("纯对话会话不接收工作目录或本地工具。")
        elif self.prompt_mode != "work" or not isinstance(self.workspace, str) or not self.workspace:
            raise ValueError("会话模式须为 dialogue 或 work；工作模式需要工作目录。")
        if not isinstance(self.system, str) or not isinstance(self.tool_names, list) or any(
            not isinstance(name, str) or not name for name in self.tool_names
        ) or len(self.tool_names) != len(set(self.tool_names)):
            raise ValueError("会话规则须为文本，工具名称须为不重复的字符串数组。")
        self.temperature = parse_temperature(self.temperature)
        self.max_output_tokens = parse_max_output_tokens(self.max_output_tokens)
        self.reasoning_effort = parse_effort(self.reasoning_effort)
        resolve_effort(
            self.config.reasoning, self.reasoning_effort, has_tools=bool(self.tool_names),
            temperature=self.temperature, max_output_tokens=self.max_output_tokens,
        )
        self.tool_names = list(self.tool_names)
        if self.shell_config is not None and (not isinstance(self.shell_config, ShellConfig) or "run_command" not in self.tool_names):
            raise ValueError("会话 Shell 配置需要实际开放 run_command。")
        if self.search_config is not None and (
            not isinstance(self.search_config, SearchConfig) or not SEARCH_TOOLS.intersection(self.tool_names)
        ):
            raise ValueError("会话搜索配置需要实际开放 find_files 或 search_text。")
        if not isinstance(self.program_paths, dict) or self.program_paths.keys() - {"python", "git"} or any(
            path is not None and (not isinstance(path, str) or not Path(path).is_absolute()) for path in self.program_paths.values()
        ):
            raise ValueError("会话附加程序仅接受 python／git 的绝对路径。")
        self.program_paths = dict(self.program_paths)

    @property
    def model_target(self) -> ModelTarget:
        """旧header缺少来源ID时仍能还原创建时的实际连接。"""
        return ModelTarget(self.config, self.model_id, self.connection_id, self.model_label, self.connection_label)

    @classmethod
    def from_dict(cls, data: dict) -> "ConversationSettings":
        """文件边界还原类型；Provider 不承担会话记录解析。"""
        return cls(**{
            **data, "config": ModelConfig(**data["config"]),
            "prompt_config": PromptConfig(**data["prompt_config"]),
            "limits": AgentLimits(**data["limits"]),
            "context_policy": ContextPolicy(**data.get("context_policy", {})),
            "shell_config": ShellConfig(**data["shell_config"]) if data.get("shell_config") is not None else None,
            # 旧记录没有搜索配置时沿用原实现，不因今天 PATH 里有 rg 就换后端。
            "search_config": (
                SearchConfig(**data["search_config"]) if data.get("search_config") is not None else None
            ) if "search_config" in data else (
                SearchConfig(backend="python") if SEARCH_TOOLS.intersection(data["tool_names"]) else None
            ),
        })


@dataclass
class ConversationTurn:
    run_id: str
    user: ModelMessage | None
    limits: AgentLimits
    started_at: str
    steps: list[AgentStep] = field(default_factory=list)
    stop_reason: str = "running"
    error: str | None = None
    diagnostics: str | None = None
    finished_at: str | None = None
    request_tokens_estimate: list[int] = field(default_factory=list)
    usage_summary: dict | None = None
    compaction_usage_summary: dict | None = None
    total_usage_summary: dict | None = None
    execution_environment: dict | None = None
    options: TurnOptions | None = None
    source_input_id: str | None = None
    resumed_from_run_id: str | None = None
    continuation: str | None = None
    retained_input_ids: list[str] = field(default_factory=list)
    regeneration_notice: str | None = None
    origin: str = "user"


@dataclass(frozen=True)
class ConversationInput:
    """一次持久接收的用户原文；使用位置由轮次和步骤引用，不重复存正文。"""

    input_id: str
    kind: str
    message: ModelMessage
    origin_run_id: str
    accepted_at: str = ""
    options: TurnOptions | None = None

    def __post_init__(self) -> None:
        if UUID(self.input_id).hex != self.input_id or UUID(self.origin_run_id).hex != self.origin_run_id:
            raise ValueError("输入和目标运行须使用稳定UUID。")
        if (self.kind not in {"steer", "follow_up"} or not isinstance(self.message, ModelMessage)
                or self.message.role != "user" or self.message.runtime_context_id):
            raise ValueError("收件只接受补充或排队的普通用户消息。")
        if not self.message.text.strip() and not any(part.type == "image" for part in self.message.content):
            raise ValueError("输入须包含文字或图片引用。")
        if self.kind == "follow_up" and self.options is None:
            raise ValueError("排队消息需要提交时的选项快照。")
        if self.kind == "steer" and self.options is not None:
            raise ValueError("补充沿当前运行的选项，不单独覆盖强度。")


@dataclass
class CompactionRecord:
    """一次摘要尝试；只有 succeeded 才改变请求视图，覆盖数是原始轮次累计数。"""

    compaction_id: str
    covered_turns: int
    through_run_id: str
    trigger: str
    started_at: str
    before_tokens_estimate: int
    owner_run_id: str | None = None
    status: str = "running"
    summary: str = ""
    after_tokens_estimate: int | None = None
    summary_input_estimate: int | None = None
    duration_seconds: float | None = None
    usage: dict | None = None
    token_usage: TokenUsage | None = None
    completion_status: str | None = None
    finish_reason: str | None = None
    error: str | None = None
    finished_at: str | None = None
    attempts: list[ModelAttemptEvent] = field(default_factory=list)
    after_step: int = 0
    model_target: ModelTarget | None = None
    summary_requests: list[dict] = field(default_factory=list)
    input_target_tokens: int | None = None
    input_estimate_scale: float = 1.0
    retained_turns: int | None = None

    def __post_init__(self) -> None:
        if self.input_target_tokens is not None and (type(self.input_target_tokens) is not int or self.input_target_tokens < 1):
            raise ValueError("压缩总输入目标须为正整数或未知值。")
        if type(self.input_estimate_scale) not in {int, float} or not isfinite(self.input_estimate_scale) or self.input_estimate_scale <= 0:
            raise ValueError("压缩输入估算比例须为有限正数。")
        if self.retained_turns is not None and (type(self.retained_turns) is not int or self.retained_turns < 0):
            raise ValueError("压缩保留轮次须为非负整数或未知值。")
        if isinstance(self.model_target, dict):
            try:
                self.model_target = ModelTarget(**self.model_target)
            except TypeError as exc:
                raise ValueError("摘要模型快照字段不正确。") from exc
        if self.model_target is not None and not isinstance(self.model_target, ModelTarget):
            raise ValueError("摘要目标须为模型连接快照。")
        if not isinstance(self.summary_requests, list) or len(self.summary_requests) > 2:
            raise ValueError("摘要生成最多保存两次请求用量。")
        for request in self.summary_requests:
            if not isinstance(request, dict) or request.get("completion_status") not in {"completed", "incomplete", "requires_tool", "unknown"}:
                raise ValueError("摘要请求终态无效。")
            raw = request.get("token_usage")
            if raw is not None and (not isinstance(raw, dict) or any(value is not None and (type(value) is not int or value < 0) for value in raw.values())):
                raise ValueError("摘要请求用量须为非负整数或未知值。")
            if raw is not None:
                TokenUsage(**raw)
            seconds = request.get("duration_seconds")
            if seconds is not None and (type(seconds) not in {int, float} or not isfinite(seconds) or seconds < 0):
                raise ValueError("摘要请求耗时无效。")

    def display_status(self) -> CompactionStatus:
        """传递位置、终态及最多两次摘要用量，不复制正文或联网重试明细。"""
        return CompactionStatus(self.compaction_id, self.trigger, self.status, self.after_step, self.error,
            self.after_tokens_estimate, self.token_usage, tuple(dict(item) for item in self.summary_requests),
            any(item.phase in {"failed", "cancelled"} for item in self.attempts), self.finished_at, self.owner_run_id)


@dataclass
class Conversation:
    conversation_id: str
    settings: ConversationSettings
    created_at: str
    turns: list[ConversationTurn] = field(default_factory=list)
    last_seq: int = 0
    compactions: list[CompactionRecord] = field(default_factory=list)
    runtime_baseline: ModelMessage | None = None
    inputs: list[ConversationInput] = field(default_factory=list)
    navigation: dict | None = None
    active_prompt: PromptConfig | None = None
    active_preset_id: str | None = None
    active_preset_name: str | None = None
    prompt_applied_at: str | None = None

    @property
    def current_prompt(self) -> PromptConfig:
        return self.active_prompt or self.settings.prompt_config

    @property
    def current_preset_id(self) -> str | None:
        return self.active_preset_id if self.active_prompt is not None else self.settings.prompt_preset_id

    @property
    def current_preset_name(self) -> str:
        name = self.active_preset_name if self.active_prompt is not None else self.settings.prompt_preset_name
        return name or "原会话预设"

    @property
    def last_options(self) -> TurnOptions:
        """已有执行事实的最新目标；候选目录不参与恢复。"""
        for turn in reversed(self.turns):
            if turn.options is not None:
                return TurnOptions(turn.options.reasoning_effort, turn.options.model_target or self.settings.model_target, self.current_prompt)
        return TurnOptions(self.settings.reasoning_effort, self.settings.model_target, self.current_prompt)

    def pending_inputs(self, kind: str | None = None) -> tuple[ConversationInput, ...]:
        """请求确实开始才使用补充；只准备而未发送的输入仍可继续。"""
        if not hasattr(self, "_consumed_inputs"):
            self.rebuild_input_index()
        return tuple(item for item in self.inputs if item.input_id not in self._consumed_inputs and (kind is None or item.kind == kind))

    def rebuild_input_index(self) -> None:
        """旧日志首次读取及历史修订时重建；派生索引不进入日志字段。"""
        used = {turn.source_input_id for turn in self.turns if turn.source_input_id}
        used.update(identifier for turn in self.turns for identifier in turn.retained_input_ids)
        used.update(identifier for turn in self.turns for step in turn.steps
                    if step.assistant is not None or any(event.phase == "started" for event in step.attempts)
                    for identifier in step.input_ids)
        self._consumed_inputs = frozenset(used)
        self._consumed_programs = frozenset(identifier for turn in self.turns for step in turn.steps for identifier in step.program_event_ids)

    @property
    def consumed_program_ids(self):
        if not hasattr(self, "_consumed_programs"):
            self.rebuild_input_index()
        return self._consumed_programs

    def update_input_index(self, kind: str) -> None:
        """只合并本次事件已消费的编号；不可变集合可安全共享给检查点候选。"""
        if kind in {"history_seed", "history_revised"} or not hasattr(self, "_consumed_inputs"):
            self.rebuild_input_index()
            return
        if not self.turns:
            return
        turn = self.turns[-1]
        if kind == "turn_started" and turn.source_input_id:
            self._consumed_inputs = self._consumed_inputs | {turn.source_input_id}
        if kind in {"model_started", "model_attempt", "model_finished"} and turn.steps:
            step = turn.steps[-1]
            self._consumed_programs = self._consumed_programs | frozenset(step.program_event_ids)
            if step.assistant is not None or any(event.phase == "started" for event in step.attempts):
                self._consumed_inputs = self._consumed_inputs | frozenset(step.input_ids)

    @property
    def resumable_turn(self) -> ConversationTurn | None:
        turn = self.turns[-1] if self.turns else None
        return turn if turn is not None and turn.stop_reason not in {"completed", "rewound"} else None

    @property
    def active_turn(self) -> ConversationTurn | None:
        """文件中没有终态的最后一轮，不代表它仍有活着的执行进程。"""
        return self.turns[-1] if self.turns and self.turns[-1].finished_at is None else None

    @property
    def active_compaction(self) -> CompactionRecord | None:
        return self.compactions[-1] if self.compactions and self.compactions[-1].finished_at is None else None

    @property
    def latest_compaction(self) -> CompactionRecord | None:
        return next((record for record in reversed(self.compactions) if record.status == "succeeded"), None)


@dataclass(frozen=True)
class ConversationSummary:
    """目录所需的事实摘要；不复制正文、工具结果、密钥或运行状态。"""

    conversation_id: str
    workspace: str | None
    created_at: str
    updated_at: str
    first_text: str
    turn_count: int
    last_seq: int
    journal_size: int | None = None
    journal_mtime_ns: int | None = None
    branch_title: str | None = None

    @classmethod
    def from_conversation(cls, conversation: Conversation) -> "ConversationSummary":
        latest = conversation.created_at
        for records in (conversation.turns, conversation.compactions):
            for record in records:
                latest = max(latest, record.started_at, record.finished_at or record.started_at)
        latest = max([latest, *(item.accepted_at for item in conversation.inputs)])
        latest = max(latest, (conversation.navigation or {}).get("time", latest))
        return cls(
            conversation.conversation_id, conversation.settings.workspace, conversation.created_at,
            latest, next((turn.user.text for turn in conversation.turns if turn.user is not None), ""),
            len(conversation.turns), conversation.last_seq,
            branch_title=(conversation.navigation or {}).get("branch_title"),
        )
