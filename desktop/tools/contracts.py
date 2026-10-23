"""工具的声明、执行输入与可反馈给模型的业务错误。"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Any, Protocol

from ..llm.messages import SkillContent, ToolSchema
from .command.shells import ShellConfig
from .filesystem.search_config import SearchConfig
from .filesystem.limits import MAX_TOOL_SECONDS


class SubagentAccess(Protocol):
    """作用域在应用中固定；模型只能传任务资料，不能传凭据或目录。"""
    profiles: dict[str, dict[str, Any]]
    async def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...
    def for_call(self, call_id: str) -> "SubagentAccess": ...


class SkillAccess(Protocol):
    """技能目录由应用选定，参数只定位其中的技能及附属资料。"""
    roots: tuple[tuple[str, str], ...]
    def load(self, identifier: str, *, manual: bool = False) -> SkillContent: ...
    def restore(self, messages) -> None: ...
    async def read_resource(self, identifier: str, path: str, cursor: dict | None = None) -> dict[str, Any]: ...


class McpAccess(Protocol):
    """运行绑定的外部连接入口；SDK对象只在MCP适配层使用。"""
    async def call(self, server_id: str, tool: dict, arguments: dict) -> Any: ...


@dataclass(frozen=True)
class ToolContext:
    """访问范围由应用确定，模型参数不能改变工作目录或可用工具。"""

    workspace: Path
    allowed_tools: frozenset[str]
    shell: ShellConfig | None = None
    artifacts_dir: Path | None = None
    excluded_env: frozenset[str] = frozenset()
    call_id: str = ""
    search_config: SearchConfig | None = None
    execution_details: dict[str, Any] | None = None
    # 仅历史真实结果和本次本地执行登记的调用可查收据，模型参数不能扩大范围。
    artifact_calls: set[str] = field(default_factory=set, compare=False)
    artifact_ids: set[str] = field(default_factory=set, compare=False)
    # 原始进程事实只写收据，与模型可见的错误details分开。
    audit_details: dict[str, Any] | None = None
    subagents: SubagentAccess | None = None
    mutation_lock: Path | None = None
    skills: SkillAccess | None = None
    # 只有挂载真实用户界面的主运行才绑定；不会给子Agent或无界面运行开放。
    question_frequency: str | None = None
    mcp: McpAccess | None = None
    # 仅本地资料范围；子任务沿用父会话编号，不进入模型请求或权限参数。
    reference_scope_id: str | None = None


ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolDisplay:
    """本地展示语义，不含Qt对象，也不加入模型的工具Schema。"""

    category: str = "tool"
    icon: str = "plugin"
    verb: str = "调用"
    summary: str = "调用了{n}个工具"
    detail_kind: str = "json"
    heading: str = "工具"
    subject_fields: tuple[str, ...] = ("path", "query", "pattern")
    subject_separator: str | None = None
    # 成功结果可提供可读名称；未返回前仍按调用参数显示，不由UI猜测。
    result_subject_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value for value in (
                self.category, self.icon, self.verb, self.summary, self.detail_kind, self.heading)):
            raise ValueError("工具展示分类、图标、文案和详情类型须为非空字符串。")
        if not isinstance(self.subject_fields, tuple) or any(not isinstance(value, str) or not value for value in self.subject_fields):
            raise ValueError("工具标题字段须为字符串元组。")
        if not isinstance(self.result_subject_fields, tuple) or any(not isinstance(value, str) or not value for value in self.result_subject_fields):
            raise ValueError("工具结果标题字段须为字符串元组。")
        if self.subject_separator is not None and not isinstance(self.subject_separator, str):
            raise ValueError("工具标题分隔符须为字符串或None。")
        try:
            self.summary.format(n=1)
        except (KeyError, ValueError, IndexError) as exc:
            raise ValueError("工具汇总文案须能使用n计数格式化。") from exc


@dataclass(frozen=True)
class ToolDefinition:
    """Schema 交给模型，处理函数只保留在本地注册表中。"""

    schema: ToolSchema
    execute: ToolHandler
    display: ToolDisplay = field(default_factory=ToolDisplay)
    # None表示由实现管理时限；Shell已有所选解释器的实际执行时限。
    timeout_seconds: float | None = MAX_TOOL_SECONDS
    default_profiles: tuple[str, ...] = ()
    execution_class: str = "exclusive"

    def __post_init__(self) -> None:
        if not isinstance(self.schema, ToolSchema) or not callable(self.execute):
            raise ValueError("工具须提供ToolSchema与可调用的execute。")
        if not isinstance(self.display, ToolDisplay):
            raise ValueError("工具展示信息须为ToolDisplay。")
        if self.execution_class not in {"read", "exclusive", "control"}:
            raise ValueError("工具执行类别须为read／exclusive／control。")
        if self.timeout_seconds is not None and (isinstance(self.timeout_seconds, bool)
                or not isinstance(self.timeout_seconds, (int, float))
                or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise ValueError("工具执行时限须为正数或None。")
        if not isinstance(self.default_profiles, tuple) or any(
                not isinstance(profile, str) or not profile for profile in self.default_profiles
        ) or len(self.default_profiles) != len(set(self.default_profiles)):
            raise ValueError("工具默认配置名称须为不重复的字符串元组。")


class ToolError(ValueError):
    """参数、访问范围或资源问题；可以反馈给模型修正调用。"""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


class ToolExecutionUnknown(RuntimeError):
    """工具副作用无法确认；保留该调用结果，并阻止继续执行后续副作用。"""

    def __init__(self, result) -> None:
        super().__init__("工具执行结果未知，已停止后续调用，请先核对收据和实际文件。")
        self.tool_result = result
