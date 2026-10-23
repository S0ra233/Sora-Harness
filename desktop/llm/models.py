"""调用方与模型后端之间传递的数据，不在这里执行网络操作。"""
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .messages import (
    Message, ModelMessage, ReplayState, TextContent, ToolCall, ToolResult,
    ToolSchema, message_from_dict, validate_tool_results,
)
from .usage import TokenUsage
from .rules import API_STYLES, RequestRules, parse_request_rules
from .reasoning import ReasoningProfile, parse_effort, reasoning_profile_from_dict


@dataclass
class ModelConfig:
    """保存非敏感连接与已确认模型能力；Gemini 地址用 {model} 表示模型路径。"""
    endpoint_url: str
    model: str
    timeout_seconds: float = 180.0
    # 默认值让此前只有三个字段的配置和运行记录继续可读。
    api_style: str = "chat_completions"
    key_env: str = "MODEL_API_KEY"
    connect_timeout_seconds: float = 30.0
    max_attempts: int = 5
    provider_kind: str = "compatible"
    # None 表示尚未确认，不能据厂商或模型名称自动开启视觉能力。
    image_input: bool | None = None
    # 用户连接设置不固化规则；运行记录和新会话保存解析后的快照。
    provider_rules: RequestRules | None = None
    # 仅保存容量资料；自动压缩仍由 ContextPolicy 显式启用。
    context_window: int | None = None
    reasoning: ReasoningProfile | None = None
    # 非敏感系统凭据引用；旧配置缺字段时仍使用key_env。
    credential_ref: str | None = None

    def __post_init__(self) -> None:
        """只还原已存快照，不为查看旧记录读取今天的提供商配置。"""
        if isinstance(self.provider_rules, dict):
            self.provider_rules = parse_request_rules(self.provider_rules, self.api_style)
        if isinstance(self.reasoning, dict):
            self.reasoning = reasoning_profile_from_dict(self.reasoning)


@dataclass
class ModelRequest:
    messages: list[Message]
    # 未设置的参数不发送，让所选模型使用自己的默认值。
    temperature: float | None = None
    max_output_tokens: int | None = None
    tools: list[ToolSchema] = field(default_factory=list)
    reasoning_effort: str | None = None
    # 声明可以复用，但辅助摘要必须禁止调用；None沿用协议默认行为。
    tool_choice: str | None = None

    # 指向不可变基线；新增资料只追加并随检查点保存，不扫描或删除用户正文。
    runtime_context_id: str | None = None
    runtime_context_updates: list[ModelMessage] = field(default_factory=list)

    def __post_init__(self) -> None:
        """在输入边界统一新对象和旧字典，Provider 不重复还原消息。"""
        if not isinstance(self.messages, list) or not self.messages or not isinstance(self.tools, list):
            raise ValueError("模型请求需要非空 messages 数组和 tools 数组。")
        self.messages = [message_from_dict(value) if isinstance(value, dict) else value for value in self.messages]
        if not isinstance(self.runtime_context_updates, list):
            raise ValueError("程序上下文更新需要消息数组。")
        self.runtime_context_updates = [message_from_dict(value) if isinstance(value, dict) else value
                                        for value in self.runtime_context_updates]
        self.tools = [ToolSchema(**value) if isinstance(value, dict) else value for value in self.tools]
        if any(not isinstance(value, ModelMessage) or value.role != "user" for value in self.runtime_context_updates):
            raise ValueError("程序上下文更新需要user资料消息。")
        if not all(isinstance(value, (ModelMessage, ToolResult)) for value in self.messages):
            raise ValueError("模型消息必须是 ModelMessage 或 ToolResult。")
        if not all(isinstance(value, ToolSchema) for value in self.tools):
            raise ValueError("模型工具声明必须是 ToolSchema。")
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ValueError("本次请求不能重复声明同名工具。")
        validate_tool_results(self.messages)
        self.reasoning_effort = parse_effort(self.reasoning_effort)
        if self.tool_choice not in (None, "none"):
            raise ValueError("tool_choice 当前只接受 null 或 none。")


@dataclass
class ModelResponse:
    text: str
    usage: dict[str, Any] | None = None
    # 收到响应不代表模型正常生成完毕，因此保留停止原因。
    finish_reason: str | None = None
    # 调用已返回与内容已完成分开；停止原因仍保留各协议的原始值。
    completion_status: str = "completed"
    # 保留原生输出供记录查阅；不能当作跨协议通用消息自动续接。
    native_data: dict[str, Any] | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    replay_state: ReplayState | None = None
    token_usage: TokenUsage | None = None

    def as_message(self) -> ModelMessage:
        """生成可续接的 assistant 消息；不把截断或未知状态加入执行历史。"""
        if self.completion_status not in ("completed", "requires_tool"):
            raise ValueError("模型输出未完整结束，不能自动续接或执行其中的工具请求。")
        return ModelMessage(
            role="assistant", content=[TextContent(self.text)] if self.text else [],
            tool_calls=deepcopy(self.tool_calls), replay_state=deepcopy(self.replay_state),
        )
