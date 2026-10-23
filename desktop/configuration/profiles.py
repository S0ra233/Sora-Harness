"""读取一次内部适配目录，按服务、协议与模型生成运行配置。"""
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ..llm.models import ModelConfig
from ..llm.reasoning import reasoning_profile_from_dict
from ..llm.rules import API_STYLES, ProviderProfile, ProviderProtocol, parse_request_rules
from ..llm.validation import validate_model_config
from .models import APIConnection, ConnectionCatalog, ModelDefinition, validate_overrides
from ..llm.model_catalog import ModelCatalog, ModelEntry


DEFAULT_ADAPTERS_PATH = Path(__file__).with_name("adapters.json")


def _read_json(path: Path) -> dict:
    try:
        with path.open("rb") as source:
            raw = source.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError("适配资料超过64 KiB。")
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            raise ValueError("适配资料须为version=1的对象。")
        return data
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"无法读取适配资料 {path}：{exc}") from exc


def parse_providers(providers: dict) -> dict[str, ProviderProfile]:
    """新目录与旧providers.json显式路径共用规则解析。"""
    if not isinstance(providers, dict) or "compatible" not in providers:
        raise ValueError("providers须为对象，并保留compatible。")
    result = {}
    for kind, data in providers.items():
        location = f"providers.{kind}"
        if re.fullmatch(r"[a-z][a-z0-9_]*", kind) is None:
            raise ValueError(f"{location}：提供商ID以小写字母开头，只接受字母、数字、下划线。")
        if not isinstance(data, dict) or data.keys() != {"label", "protocols"}:
            raise ValueError(f"{location}只接受label和protocols。")
        if not isinstance(data["label"], str) or not data["label"].strip():
            raise ValueError(f"{location}.label不能为空。")
        protocols = data["protocols"]
        if not isinstance(protocols, dict) or not protocols:
            raise ValueError(f"{location}.protocols需要至少一个协议。")
        parsed = {}
        for style, value in protocols.items():
            target = f"{location}.protocols.{style}"
            if style not in API_STYLES or not isinstance(value, dict) or value.keys() != {"endpoint_example", "request_rules"}:
                raise ValueError(f"{target}需要已实现的协议、endpoint_example和request_rules。")
            example = value["endpoint_example"]
            if not isinstance(example, str) or not example.startswith(("https://", "http://")):
                raise ValueError(f"{target}.endpoint_example须为HTTP地址示例。")
            parsed[style] = ProviderProtocol(example, parse_request_rules(
                value["request_rules"], style, location=f"{target}.request_rules",
            ))
        result[kind] = ProviderProfile(data["label"], parsed)
    return result


def load_legacy_provider_profiles(path: Path | str) -> dict[str, ProviderProfile]:
    path = Path(path)
    data = _read_json(path)
    if data.keys() != {"version", "providers"}:
        raise ValueError(f"旧提供商目录 {path}只接受version和providers。")
    return parse_providers(data["providers"])


@dataclass(frozen=True)
class ModelRule:
    identifier: str
    provider_kind: str
    api_style: str
    models: tuple[str, ...]
    model_prefixes: tuple[str, ...]
    capabilities: dict

    def priority(self, model: str) -> int:
        if model in self.models:
            return 3
        if any(prefix != "*" and model.startswith(prefix) for prefix in self.model_prefixes):
            return 2
        # 星号明确表示协议通用预设；模型／系列规则仍优先于通用值。
        return 1 if "*" in self.model_prefixes else 0


@dataclass(frozen=True)
class AdapterCatalog:
    providers: dict[str, ProviderProfile]
    defaults: dict
    model_rules: tuple[ModelRule, ...]

    def capabilities(self, provider_kind: str, api_style: str, model: str) -> dict:
        matched = [(rule.priority(model), rule) for rule in self.model_rules
                   if rule.provider_kind == provider_kind and rule.api_style == api_style and rule.priority(model)]
        best = max((priority for priority, _ in matched), default=0)
        winners = [rule for priority, rule in matched if priority == best]
        if len(winners) > 1:
            raise ValueError(f"模型 {provider_kind}/{api_style}/{model} 匹配了同级规则："
                             + "、".join(rule.identifier for rule in winners))
        return deepcopy({**self.defaults, **(winners[0].capabilities if winners else {})})


def load_adapters(path: Path | str = DEFAULT_ADAPTERS_PATH) -> AdapterCatalog:
    """只在配置操作入口加载；语法／协议错误带文件和字段位置，不静默回退。"""
    path = Path(path)
    data = _read_json(path)
    try:
        if data.keys() != {"version", "defaults", "providers", "reasoning_profiles", "model_rules"}:
            raise ValueError("顶层只接受version、defaults、providers、reasoning_profiles和model_rules。")
        providers = parse_providers(data["providers"])
        defaults = data["defaults"]
        validate_overrides(defaults)
        profiles = data["reasoning_profiles"]
        if not isinstance(profiles, dict) or not isinstance(data["model_rules"], list):
            raise ValueError("reasoning_profiles须为对象，model_rules须为数组。")
        parsed_profiles = {}
        for name, value in profiles.items():
            if not name.strip():
                raise ValueError("reasoning_profiles的模板名不能为空。")
            try:
                parsed_profiles[name] = reasoning_profile_from_dict(value)
            except ValueError as exc:
                raise ValueError(f"reasoning_profiles.{name}：{exc}") from exc
        rules, identifiers, exact = [], set(), set()
        for index, value in enumerate(data["model_rules"]):
            location = f"model_rules[{index}]"
            if not isinstance(value, dict) or value.keys() - {
                "id", "provider_kind", "api_style", "models", "model_prefixes", "capabilities", "reasoning_profile",
            } or not {"id", "provider_kind", "api_style"}.issubset(value):
                raise ValueError(f"{location}包含未知字段或缺少id／服务／协议。")
            identifier, kind, style = value["id"], value["provider_kind"], value["api_style"]
            if not isinstance(identifier, str) or not identifier or identifier in identifiers:
                raise ValueError(f"{location}.id须为不重复的非空文本。")
            if not isinstance(kind, str) or kind not in providers or not isinstance(style, str) or style not in providers[kind].protocols:
                raise ValueError(f"{location}的服务／协议未配置。")
            models, prefixes = value.get("models", []), value.get("model_prefixes", [])
            if any(not isinstance(items, list) or any(not isinstance(name, str) or not name for name in items)
                   for items in (models, prefixes)) or not (models or prefixes):
                raise ValueError(f"{location}需要非空models或model_prefixes数组。")
            capabilities = deepcopy(value.get("capabilities", {}))
            validate_overrides(capabilities)
            if "reasoning_profile" in value:
                name = value["reasoning_profile"]
                if not isinstance(name, str) or name not in parsed_profiles or "reasoning" in capabilities:
                    raise ValueError(f"{location}.reasoning_profile未定义或与reasoning覆盖值重复。")
                capabilities["reasoning"] = parsed_profiles[name]
            reasoning = capabilities.get("reasoning", defaults.get("reasoning"))
            if reasoning is not None:
                if isinstance(reasoning, dict):
                    reasoning = reasoning_profile_from_dict(reasoning)
                try:
                    reasoning.validate(style)
                except ValueError as exc:
                    raise ValueError(f"{location} ({identifier})：{exc}") from exc
            for model in models:
                key = kind, style, model
                if key in exact:
                    raise ValueError(f"{location}与其他规则重复匹配 {key}。")
                exact.add(key)
            identifiers.add(identifier)
            rules.append(ModelRule(identifier, kind, style, tuple(models), tuple(prefixes), capabilities))
        return AdapterCatalog(providers, deepcopy(defaults), tuple(rules))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"适配资料 {path}：{exc}") from exc


def endpoint_url(connection: APIConnection) -> str:
    """源配置保存用户地址；运行层仍收到完整请求URL，保留旧完整地址。"""
    return resolve_endpoint_url(connection.api_url, connection.api_style, connection.url_mode)


def resolve_endpoint_url(api_url: str, api_style: str, url_mode: str) -> str:
    """编辑页预览和运行解析共用同一条地址规则，不发送网络请求。"""
    url = api_url.strip()
    if url_mode == "endpoint":
        return url
    address = urlsplit(url)
    path = address.path.rstrip("/")
    if api_style in ("chat_completions", "responses"):
        suffix = "/chat/completions" if api_style == "chat_completions" else "/responses"
        if not path.endswith(suffix):
            path += suffix
    elif api_style == "anthropic_messages":
        if not path.endswith("/messages"):
            path += "/messages" if path.endswith("/v1") else "/v1/messages"
    elif api_style == "gemini":
        if not path.endswith((":generateContent", ":streamGenerateContent")):
            path += "/models/{model}:generateContent" if path.endswith(("/v1beta", "/v1")) else "/v1beta/models/{model}:generateContent"
    return urlunsplit((address.scheme, address.netloc, path, address.query, address.fragment))


def resolve_connection_model(connection: APIConnection, definition: ModelDefinition, adapters: AdapterCatalog) -> ModelConfig:
    profile = adapters.providers.get(connection.provider_kind)
    if profile is None or connection.api_style not in profile.protocols:
        raise ValueError(f"提供商 {connection.provider_kind} 未配置协议 {connection.api_style}。")
    capabilities = adapters.capabilities(connection.provider_kind, connection.api_style, definition.model)
    overrides = deepcopy(definition.overrides)
    if connection.provider_kind == "compatible" and overrides.get("reasoning") is None:
        # 旧目录的未知值继承用户现已选择的兼容协议预设；显式unsupported仍保留。
        overrides.pop("reasoning", None)
    capabilities.update(overrides)
    config = ModelConfig(
        endpoint_url=endpoint_url(connection), model=definition.model, api_style=connection.api_style,
        provider_kind=connection.provider_kind, credential_ref=connection.credential_ref, key_env=connection.key_env,
        timeout_seconds=connection.timeout_seconds, connect_timeout_seconds=connection.connect_timeout_seconds,
        max_attempts=connection.max_attempts, provider_rules=profile.protocols[connection.api_style].request_rules,
        **capabilities,
    )
    validate_model_config(config)
    return config


def model_catalog(catalog: ConnectionCatalog, adapters: AdapterCatalog) -> ModelCatalog:
    """把连接目录投影成既有选择契约，条目保留接口归属。"""
    models = {model_id: ModelEntry(definition.label, resolve_connection_model(connection, definition, adapters),
                                  connection_id, connection.label)
              for connection_id, connection in catalog.connections.items()
              for model_id, definition in connection.models.items()}
    return ModelCatalog(models, catalog.active_model)
