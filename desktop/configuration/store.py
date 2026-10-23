"""非敏感接口目录读写；旧配置只在内存转换，明确保存才写新版。"""
import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import NAMESPACE_URL, uuid5

from ..llm.model_catalog import ModelCatalog, ModelEntry
from ..llm.models import ModelConfig
from .models import APIConnection, ConnectionCatalog, ModelDefinition


def _legacy_config(data: dict) -> ModelConfig:
    if not isinstance(data, dict):
        raise ValueError("旧模型config须为对象。")
    try:
        return ModelConfig(**{**data, "provider_rules": None})
    except TypeError as exc:
        raise ValueError("旧模型配置含未知字段或缺少endpoint_url／model。") from exc


def from_model_catalog(catalog: ModelCatalog) -> ConnectionCatalog:
    """兼容旧平铺视图；稳定旧模型ID，不猜测多个旧条目是否共用账号。"""
    connections = {}
    active_connection = None
    for model_id, entry in catalog.models.items():
        config = entry.config
        connection_id = entry.connection_id or uuid5(NAMESPACE_URL, "desktop-model:" + model_id).hex
        model = ModelDefinition(config.model, entry.label, {
            "image_input": config.image_input, "context_window": config.context_window,
            "reasoning": asdict(config.reasoning) if config.reasoning is not None else None,
        })
        connection = APIConnection(
            label=entry.connection_label or entry.label, api_url=config.endpoint_url,
            url_mode="endpoint", provider_kind=config.provider_kind, api_style=config.api_style,
            credential_ref=config.credential_ref, key_env=config.key_env,
            timeout_seconds=config.timeout_seconds, connect_timeout_seconds=config.connect_timeout_seconds,
            max_attempts=config.max_attempts, models={model_id: model}, default_model=model_id,
        )
        if connection_id in connections:
            previous = connections[connection_id]
            if replace(previous, models=connection.models, default_model=model_id) != connection:
                raise ValueError("同一接口的旧模型视图含不同连接值，不能覆盖已有接口。")
            connection = replace(previous, models={**previous.models, model_id: model},
                                 default_model=model_id if model_id == catalog.active_model else previous.default_model)
        connections[connection_id] = connection
        if model_id == catalog.active_model:
            active_connection = connection_id
    return ConnectionCatalog(connections, active_connection, catalog.active_model)


def load_connections(path: Path) -> ConnectionCatalog:
    try:
        content = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return ConnectionCatalog()
    try:
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError("配置须为对象。")
        if "version" not in data:
            config = _legacy_config(data)
            return from_model_catalog(ModelCatalog({"default": ModelEntry(config.model, config)}, "default"))
        if type(data["version"]) is not int:
            raise ValueError("version须为整数。")
        if data["version"] == 2:
            if data.keys() != {"version", "active_model", "models"} or not isinstance(data["models"], dict):
                raise ValueError("旧目录仅接受version=2、active_model和models。")
            models = {}
            for model_id, entry in data["models"].items():
                if not isinstance(entry, dict) or entry.keys() != {"label", "config"}:
                    raise ValueError(f"旧模型条目 {model_id}仅接受label和config。")
                models[model_id] = ModelEntry(entry["label"], _legacy_config(entry["config"]))
            return from_model_catalog(ModelCatalog(models, data["active_model"]))
        if data["version"] != 3 or data.keys() != {"version", "active_connection", "active_model", "connections"}:
            raise ValueError("接口目录须使用version=3、active_connection、active_model和connections。")
        if not isinstance(data["connections"], dict):
            raise ValueError("connections须为按接口ID索引的对象。")
        connections = {}
        for identifier, value in data["connections"].items():
            if not isinstance(value, dict) or not isinstance(value.get("models"), dict):
                raise ValueError(f"接口 {identifier}需要models对象。")
            models = {}
            for key, definition in value["models"].items():
                try:
                    models[key] = ModelDefinition(**definition)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"接口 {identifier}的模型 {key}：{exc}") from exc
            try:
                connections[identifier] = APIConnection(**{**value, "models": models})
            except (TypeError, ValueError) as exc:
                raise ValueError(f"接口 {identifier}：{exc}") from exc
        return ConnectionCatalog(connections, data["active_connection"], data["active_model"])
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"模型配置 {path}：{exc}") from exc


def serialize_connections(catalog: ConnectionCatalog) -> str:
    """纯序列化先于密钥副作用，整个目录不能出现非法JSON数值。"""
    return json.dumps({"version": 3, **asdict(catalog)}, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def write_connection_content(path: Path, content: str) -> None:
    """候选完整写入并关闭后替换；失败保留旧文件和候选文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                            prefix=path.name + ".", suffix=".tmp", delete=False) as temporary:
        temporary_path = Path(temporary.name)
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
    temporary_path.replace(path)


def write_connections(path: Path, catalog: ConnectionCatalog) -> None:
    write_connection_content(path, serialize_connections(catalog))
