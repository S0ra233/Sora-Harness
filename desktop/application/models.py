"""配置操作的应用边界；校验和保存先于界面更新，运行配置保持原契约。"""
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from ..configuration.models import APIConnection, ConnectionCatalog, ConnectionDraft
from ..configuration.profiles import AdapterCatalog, endpoint_url, load_adapters, model_catalog, resolve_connection_model
from ..configuration.store import from_model_catalog, load_connections, serialize_connections, write_connection_content, write_connections
from ..credentials import resolve_api_key, save
from ..llm.model_discovery import ModelListSource, RemoteModel, discover_models, models_url
from ..llm.model_catalog import ModelCatalog, ModelEntry
from ..llm.models import ModelConfig
from ..llm.validation import resolve_model_config


class ModelSaveError(OSError):
    """兼容旧保存入口的非敏感重试信息。"""

    def __init__(self, config: ModelConfig, cause: OSError) -> None:
        super().__init__(f"密钥已保存、连接配置待保存：{cause}；请保留输入并重试保存。")
        self.config = config


class ConnectionSaveError(OSError):
    """系统密钥已保存、目录未保存；重试保留同一接口和凭据ID。"""

    def __init__(self, draft: ConnectionDraft, cause: OSError) -> None:
        super().__init__(f"密钥已保存，接口目录尚未保存：{cause}；输入保留，可以重试。")
        self.draft = draft


def _identity(connection: APIConnection) -> tuple[str, str, str]:
    return endpoint_url(connection), connection.provider_kind, connection.api_style


def _config_connection(config: ModelConfig) -> tuple:
    return (config.endpoint_url.strip(), config.provider_kind, config.api_style, config.credential_ref,
            config.key_env, config.timeout_seconds, config.connect_timeout_seconds, config.max_attempts)


class ModelSettingsService:
    """真实调用者是设置页与旧CLI／插件入口；不保存活跃任务或界面控件。"""

    def __init__(self, path: Path, *, adapters: AdapterCatalog | None = None) -> None:
        self.path = path
        self._adapters = adapters

    def adapters(self) -> AdapterCatalog:
        # 同一个设置操作对象复用解析结果；新操作对象才读取新的适配文件。
        if self._adapters is None:
            self._adapters = load_adapters()
        return self._adapters

    def list_connections(self) -> ConnectionCatalog:
        return load_connections(self.path)

    def model_catalog(self) -> ModelCatalog:
        return model_catalog(self.list_connections(), self.adapters())

    def project_catalog(self, catalog: ConnectionCatalog) -> ModelCatalog:
        """成功保存结果直接投影一次，界面通知不用重新读取源文件。"""
        return model_catalog(catalog, self.adapters())

    def get_model_config(self, connection_id: str, model_id: str) -> ModelConfig:
        catalog = self.list_connections()
        try:
            connection = catalog.connections[connection_id]
            definition = connection.models[model_id]
        except KeyError as exc:
            raise ValueError("接口或模型条目不存在，请重新加载目录。") from exc
        return resolve_connection_model(connection, definition, self.adapters())

    async def list_remote_models(self, source: ModelListSource, key_update: str = "") -> tuple[RemoteModel, ...]:
        """目录查询使用本次输入或既有凭据；查询本身不保存密钥／接口。"""
        url = models_url(source)
        key = key_update.strip() or resolve_api_key(ModelConfig(
            endpoint_url=url, model="model-list", api_style=source.api_style,
            credential_ref=source.credential_ref, key_env=source.key_env,
        ))
        return await discover_models(source, key)

    def save_connection(self, draft: ConnectionDraft, key_update: str = "", *,
                        retry_draft: ConnectionDraft | None = None) -> ConnectionCatalog:
        """先校验此接口全部模型，后写凭据与目录；不解析用户effort JSON。"""
        catalog = self.list_connections()
        identifier = draft.connection_id
        if identifier is not None and identifier not in catalog.connections:
            if retry_draft is None or identifier != retry_draft.connection_id:
                raise ValueError("要编辑的接口不存在，请重新加载目录。")
        identifier = identifier or uuid4().hex
        connection = draft.connection
        previous = catalog.connections.get(identifier)
        source = retry_draft.connection if retry_draft is not None and retry_draft.connection_id == identifier else previous
        reference = connection.credential_ref
        key = key_update.strip()
        if reference is not None and (source is None or source.credential_ref != reference or _identity(source) != _identity(connection)):
            if not key:
                raise ValueError("地址、提供商或协议已改变，请重新填写密钥后保存。")
            reference = None
        if key:
            # 新密钥独立保存；只有未提交目录的重试引用可以复用。
            reference = (reference if retry_draft is not None and source is retry_draft.connection
                         and reference is not None and (previous is None or reference != previous.credential_ref)
                         else uuid4().hex)
        if reference is None and not key and previous is None:
            raise ValueError("请填写密钥后创建接口。")
        connection = replace(connection, credential_ref=reference)
        # 保存接口即选择其默认模型，其他接口及模型原样保留。
        updated = ConnectionCatalog({**catalog.connections, identifier: connection}, identifier, connection.default_model)
        for definition in connection.models.values():
            resolve_connection_model(connection, definition, self.adapters())
        content = serialize_connections(updated)
        if key:
            save(reference, key)
        try:
            write_connection_content(self.path, content)
        except OSError as exc:
            if key:
                raise ConnectionSaveError(ConnectionDraft(connection, identifier), exc) from exc
            raise
        return updated

    def activate_connection(self, connection_id: str) -> ConnectionCatalog:
        catalog = self.list_connections()
        if connection_id not in catalog.connections:
            raise ValueError("接口不存在，请重新加载目录。")
        connection = catalog.connections[connection_id]
        resolve_connection_model(connection, connection.models[connection.default_model], self.adapters())
        updated = replace(catalog, active_connection=connection_id, active_model=connection.default_model)
        write_connections(self.path, updated)
        return updated

    def select_model(self, model_id: str) -> ModelConfig:
        catalog = self.list_connections()
        connection_id, connection, definition = catalog.find_model(model_id)
        config = resolve_connection_model(connection, definition, self.adapters())
        write_connections(self.path, replace(catalog, active_connection=connection_id, active_model=model_id))
        return config


def load_model_catalog(path: Path) -> ModelCatalog:
    """兼容旧模型选择入口；一个读取操作只解析一次适配文件。"""
    return ModelSettingsService(path).model_catalog()


def load_settings(path: Path, *, model_id: str | None = None) -> ModelConfig | None:
    entry = load_model_catalog(path).selected(model_id)
    return entry.config if entry is not None else None


def select_model(path: Path, model_id: str) -> ModelConfig:
    return ModelSettingsService(path).select_model(model_id)


def update_model_catalog(catalog: ModelCatalog, config: ModelConfig, *, label: str | None = None,
                         model_id: str | None = None, new_model: bool = False) -> ModelCatalog:
    """兼容旧调用的纯准备步骤，保留指定条目ID和其他模型。"""
    config = resolve_model_config(replace(config, provider_rules=None))
    if new_model and model_id is not None:
        raise ValueError("新增模型不能同时指定编辑条目。")
    key = uuid4().hex if new_model else model_id or catalog.active_model or uuid4().hex
    if model_id is not None and model_id not in catalog.models:
        raise ValueError("要编辑的模型条目不存在。")
    previous = catalog.models.get(key)
    name = label if label is not None else previous.label if previous else config.model
    # 同连接编辑保留接口身份；单个模型换连接时拆开，避免改动兄弟模型。
    same = previous is not None and _config_connection(previous.config) == _config_connection(config)
    connection_id = (previous.connection_id if same else uuid4().hex
                     if previous is not None and previous.connection_id is not None else None)
    entry = ModelEntry(name, replace(config, provider_rules=None),
                       connection_id, previous.connection_label if same else None)
    return ModelCatalog({**catalog.models, key: entry}, key)


def _preserve_sources(view: ModelCatalog, original: ConnectionCatalog, adapters: AdapterCatalog) -> ConnectionCatalog:
    """旧入口写模型视图时保留未修改的源覆盖值、基础地址和接口默认模型。"""
    projected = from_model_catalog(view)
    connections = dict(projected.connections)
    for identifier, connection in projected.connections.items():
        old = original.connections.get(identifier)
        if old is None:
            continue
        sample = next(iter(connection.models))
        old_sample = next(iter(old.models.values()))
        if _config_connection(view.models[sample].config) != _config_connection(resolve_connection_model(old, old_sample, adapters)):
            continue
        definitions = dict(connection.models)
        for key in definitions:
            if key not in old.models:
                continue
            config = resolve_connection_model(old, old.models[key], adapters)
            if replace(config, provider_rules=None) == replace(view.models[key].config, provider_rules=None):
                definitions[key] = replace(old.models[key], label=view.models[key].label)
        connections[identifier] = replace(old, models=definitions,
                                           default_model=old.default_model if old.default_model in definitions else connection.default_model)
    return replace(projected, connections=connections)


def write_model_catalog(path: Path, catalog: ModelCatalog) -> None:
    service = ModelSettingsService(path)
    write_connections(path, _preserve_sources(catalog, service.list_connections(), service.adapters()))


def save_settings(path: Path, config: ModelConfig, *, label: str | None = None,
                  model_id: str | None = None, new_model: bool = False) -> ModelCatalog:
    service = ModelSettingsService(path)
    source = service.list_connections()
    updated = update_model_catalog(model_catalog(source, service.adapters()), config, label=label, model_id=model_id, new_model=new_model)
    write_connections(path, _preserve_sources(updated, source, service.adapters()))
    return updated


def save_model(path: Path, config: ModelConfig, api_key: str, *, label: str,
               model_id: str | None = None, new_model: bool = False,
               previous: ModelConfig | None = None, retry_config: ModelConfig | None = None) -> ModelCatalog:
    """保留旧表单／外部调用入口；新设置页使用save_connection。"""
    service = ModelSettingsService(path)
    original = service.list_connections()
    source = retry_config if retry_config is not None and config.credential_ref == retry_config.credential_ref else previous
    reference = config.credential_ref
    if reference is not None and (source is None or (config.endpoint_url.strip(), config.provider_kind, config.api_style)
                                != (source.endpoint_url.strip(), source.provider_kind, source.api_style)):
        if not api_key.strip():
            raise ValueError("连接已改变，请重新填写密钥。")
        reference = None
    if api_key.strip():
        if (reference is None or source is not retry_config or retry_config is None
                or previous is not None and reference == previous.credential_ref):
            reference = uuid4().hex
        config = replace(config, credential_ref=reference)
    updated = update_model_catalog(model_catalog(original, service.adapters()), config, label=label, model_id=model_id, new_model=new_model)
    directory = _preserve_sources(updated, original, service.adapters())
    content = serialize_connections(directory)
    if api_key.strip():
        save(reference, api_key.strip())
    try:
        write_connection_content(path, content)
    except OSError as exc:
        if api_key.strip():
            raise ModelSaveError(config, exc) from exc
        raise
    return updated
