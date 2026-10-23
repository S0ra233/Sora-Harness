"""接口与模型的源配置；真实密钥和已解析请求规则不属于这些对象。"""
from dataclasses import dataclass, field

from ..llm.reasoning import ReasoningProfile, reasoning_profile_from_dict
from ..llm.rules import API_STYLES


CAPABILITY_FIELDS = {"context_window", "image_input", "reasoning"}


def validate_overrides(values: dict) -> None:
    """缺字段采用适配资料，显式null保留旧配置的未知声明。"""
    if not isinstance(values, dict) or values.keys() - CAPABILITY_FIELDS:
        raise ValueError("模型覆盖值只接受 context_window、image_input 和 reasoning。")
    window = values.get("context_window")
    if window is not None and (type(window) is not int or window < 1):
        raise ValueError("上下文容量须为正整数或null。")
    if values.get("image_input") is not None and type(values["image_input"]) is not bool:
        raise ValueError("图片能力须为布尔值或null。")
    reasoning = values.get("reasoning")
    if isinstance(reasoning, dict):
        reasoning_profile_from_dict(reasoning)
    elif reasoning is not None and not isinstance(reasoning, ReasoningProfile):
        raise ValueError("推理覆盖值须为ReasoningProfile、对象或null。")


@dataclass(frozen=True)
class ModelDefinition:
    model: str
    label: str
    overrides: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("API模型ID不能为空。")
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("模型显示名称不能为空。")
        validate_overrides(self.overrides)


@dataclass(frozen=True)
class APIConnection:
    label: str
    api_url: str
    provider_kind: str
    api_style: str
    models: dict[str, ModelDefinition]
    default_model: str
    url_mode: str = "base"
    credential_ref: str | None = None
    key_env: str = "MODEL_API_KEY"
    timeout_seconds: float = 180.0
    connect_timeout_seconds: float = 30.0
    max_attempts: int = 5

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("接口名字不能为空。")
        if not isinstance(self.api_url, str) or not self.api_url.strip():
            raise ValueError("API URL不能为空。")
        if not isinstance(self.provider_kind, str) or not self.provider_kind.strip() or not isinstance(self.api_style, str) or self.api_style not in API_STYLES:
            raise ValueError("接口需要提供商ID和已实现的API协议。")
        if self.url_mode not in ("base", "endpoint"):
            raise ValueError("地址类型须为base或endpoint。")
        if not isinstance(self.models, dict) or not self.models or any(
            not isinstance(key, str) or not key.strip() or not isinstance(value, ModelDefinition)
            for key, value in self.models.items()
        ):
            raise ValueError("接口需要至少一个有稳定条目ID的模型。")
        if not isinstance(self.default_model, str) or self.default_model not in self.models:
            raise ValueError("默认模型须在此接口的模型列表中。")


@dataclass(frozen=True)
class ConnectionDraft:
    """编辑意图与稳定接口ID；密钥更新通过独立参数传递。"""
    connection: APIConnection
    connection_id: str | None = None


@dataclass(frozen=True)
class ConnectionCatalog:
    connections: dict[str, APIConnection] = field(default_factory=dict)
    active_connection: str | None = None
    active_model: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.connections, dict) or any(
            not isinstance(key, str) or not key.strip() or not isinstance(value, APIConnection)
            for key, value in self.connections.items()
        ):
            raise ValueError("接口目录须以非空ID对应APIConnection。")
        identifiers = [key for connection in self.connections.values() for key in connection.models]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("模型条目ID不能跨接口重复；API模型名字可以重复。")
        if not self.connections:
            if self.active_connection is not None or self.active_model is not None:
                raise ValueError("空接口目录不能声明默认选择。")
        elif (not isinstance(self.active_connection, str) or self.active_connection not in self.connections
              or not isinstance(self.active_model, str) or self.active_model not in self.connections[self.active_connection].models):
            raise ValueError("默认接口和模型必须对应同一个已保存条目。")

    def find_model(self, model_id: str) -> tuple[str, APIConnection, ModelDefinition]:
        for connection_id, connection in self.connections.items():
            if model_id in connection.models:
                return connection_id, connection, connection.models[model_id]
        raise ValueError(f"模型目录没有条目ID {model_id}。")
