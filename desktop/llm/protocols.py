"""协议目录的纯数据边界；不导入后端、模型或桌面控件。"""
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class ProtocolDefinition:
    """声明实际实现入口和原生续接外形；消息转换仍由协议实现负责。"""

    label: str
    provider_entry: str
    replay_fields: tuple[tuple[str, type], ...]


PROTOCOLS = MappingProxyType({
    "chat_completions": ProtocolDefinition("Chat Completions",
        "desktop.llm.providers.chat_completions.provider:HttpChatProvider", (("message", dict),)),
    "responses": ProtocolDefinition("OpenAI Responses",
        "desktop.llm.providers.responses:ResponsesProvider", (("output", list),)),
    "anthropic_messages": ProtocolDefinition("Anthropic Messages",
        "desktop.llm.providers.anthropic:AnthropicProvider", (("content", list),)),
    "gemini": ProtocolDefinition("Gemini Generate Content",
        "desktop.llm.providers.gemini:GeminiProvider", (("content", dict), ("call_ids", dict))),
})

# 保留旧导入位置和标签字典形状；唯一登记信息来自上面的目录。
API_STYLES = {name: definition.label for name, definition in PROTOCOLS.items()}
