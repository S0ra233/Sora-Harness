"""协议规则与类型的纯数据边界，不读取配置文件。"""
from dataclasses import dataclass
from .protocols import API_STYLES



@dataclass(frozen=True)
class RequestRules:
    """一次运行的固定规则；None 表示该协议不使用这个选项。"""

    output_limit_field: str | None = None
    stream_usage: bool | None = None
    include_encrypted_reasoning: bool | None = None
    cache_control: bool = False

    def validate(self, api_style: str) -> None:
        """仅支持代码已经实现的差异，不能通过配置覆盖 messages 或 tools。"""
        if type(self.cache_control) is not bool or self.cache_control and api_style != "anthropic_messages":
            raise ValueError("cache_control 仅适用于明确支持缓存标记的 Messages 接口。")
        if api_style == "chat_completions":
            if self.output_limit_field not in ("max_tokens", "max_completion_tokens") or type(self.stream_usage) is not bool:
                raise ValueError("Chat 规则需要 output_limit_field=max_tokens／max_completion_tokens 和布尔 stream_usage。")
            if self.include_encrypted_reasoning is not None:
                raise ValueError("Chat 规则不接受 include_encrypted_reasoning。")
        elif api_style == "responses":
            if type(self.include_encrypted_reasoning) is not bool:
                raise ValueError("Responses 规则需要布尔 include_encrypted_reasoning。")
            if self.output_limit_field is not None or self.stream_usage is not None:
                raise ValueError("Responses 规则不接受 Chat 专用字段。")
        elif api_style not in API_STYLES or any(value is not None for value in (
            self.output_limit_field, self.stream_usage, self.include_encrypted_reasoning,
        )):
            raise ValueError("Messages／Gemini 不接受 Chat／Responses 专用字段，未知协议需要代码适配。")


def parse_request_rules(data: dict, api_style: str, *, location: str = "provider_rules") -> RequestRules:
    """配置文件和历史快照共用类型转换；错误指出字段位置。"""
    try:
        if not isinstance(data, dict):
            raise ValueError("规则须为 JSON 对象。")
        if not isinstance(api_style, str) or api_style not in API_STYLES:
            raise ValueError("规则快照必须对应已实现的协议。")
        try:
            rules = RequestRules(**data)
        except TypeError as exc:
            raise ValueError("规则包含未知字段。") from exc
        rules.validate(api_style)
        return rules
    except ValueError as exc:
        raise ValueError(f"{location}：{exc}") from exc


@dataclass(frozen=True)
class ProviderProtocol:
    endpoint_example: str
    request_rules: RequestRules


@dataclass(frozen=True)
class ProviderProfile:
    label: str
    protocols: dict[str, ProviderProtocol]


