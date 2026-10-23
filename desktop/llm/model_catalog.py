"""用户保存的模型目录；条目 ID 与服务的实际模型名称分开。"""
from copy import deepcopy
from dataclasses import dataclass, field

from .models import ModelConfig
from .validation import validate_model_config


@dataclass(frozen=True)
class ModelTarget:
    """一次选择的非敏感连接快照；目录刷新不替换它。"""

    config: ModelConfig
    model_id: str | None = None
    connection_id: str | None = None
    label: str | None = None
    connection_label: str | None = None

    def __post_init__(self) -> None:
        try:
            config = ModelConfig(**self.config) if isinstance(self.config, dict) else deepcopy(self.config)
        except TypeError as exc:
            raise ValueError("模型目标的连接快照字段不正确。") from exc
        if not isinstance(config, ModelConfig):
            raise ValueError("模型目标需要ModelConfig连接快照。")
        validate_model_config(config)
        for value in (self.model_id, self.connection_id, self.label, self.connection_label):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError("模型来源ID和显示名须为非空文本或空值。")
        object.__setattr__(self, "config", config)

@dataclass(frozen=True)
class ModelEntry:
    """显示名称可以重复；连接与能力随条目保存，不保存密钥值。"""

    label: str
    config: ModelConfig
    # 兼容原来的两个位置参数；新目录视图补充接口归属供选择器展示。
    connection_id: str | None = None
    connection_label: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("模型条目的显示名称不能为空。")
        if not isinstance(self.config, ModelConfig):
            raise ValueError("模型条目需要 ModelConfig。")
        validate_model_config(self.config)


@dataclass(frozen=True)
class ModelCatalog:
    """应用的默认选择与条目集合；改变它不修改既有会话。"""

    models: dict[str, ModelEntry] = field(default_factory=dict)
    active_model: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.models, dict) or any(
            not isinstance(key, str) or not key.strip() or not isinstance(entry, ModelEntry)
            for key, entry in self.models.items()
        ):
            raise ValueError("模型目录须以非空条目 ID 对应 ModelEntry。")
        if self.active_model is not None and (
            not isinstance(self.active_model, str) or self.active_model not in self.models
        ):
            raise ValueError("active_model 必须对应已保存的模型条目。")
        if self.models and self.active_model is None:
            raise ValueError("非空模型目录需要 active_model。")

    def selected(self, model_id: str | None = None) -> ModelEntry | None:
        """按 ID 直接查找；未配置任何模型时返回 None。"""
        key = self.active_model if model_id is None else model_id
        if key is None:
            return None
        try:
            return self.models[key]
        except KeyError as exc:
            raise ValueError(f"模型目录没有条目 ID {key}。") from exc
