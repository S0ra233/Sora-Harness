"""插件与平台之间的数据和函数约定。"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from typing import TYPE_CHECKING
from threading import Event

# 旧调用方仍可从本模块导入这些类型，实际定义集中在模型层。
from ..llm.contracts import ModelCaller, ModelStreamCaller, RequestSink, TextSink

if TYPE_CHECKING:
    from ..tools.contracts import ToolContext


@dataclass(frozen=True)
class RunPreparation:
    """本次运行的本地能力；不进日志或API，不由模型参数提供。"""

    additional_tool_names: tuple[str, ...] = ()
    bind_tools: Callable[["ToolContext"], "ToolContext"] | None = None
    program_source: Callable[[], list[dict[str, Any]]] | None = None
    program_ack: Callable[[list[str]], None] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.additional_tool_names, tuple) or any(
                not isinstance(name, str) or not name for name in self.additional_tool_names):
            raise ValueError("本轮额外工具须为非空名称元组。")
        if any(value is not None and not callable(value) for value in (
                self.bind_tools, self.program_source, self.program_ack)):
            raise ValueError("本轮能力绑定与程序通知须为可调用入口。")


@dataclass
class ExecutionContext:
    """把平台能力交给插件；这里的上下文不指模型的消息历史。"""

    complete: ModelCaller
    stream_complete: ModelStreamCaller
    cancel_event: Event | None = None


# 各插件决定自己的输入字段和返回类型，执行器只负责传递。
ActionHandler = Callable[[ExecutionContext, dict[str, Any]], Awaitable[Any]]


@dataclass
class Plugin:
    """一个插件的标识，以及动作名称到处理函数的映射。"""

    plugin_id: str
    actions: dict[str, ActionHandler]
