"""命令行、配置文件和桌面表单共用的输入约束，不执行网络请求。"""
import math
from dataclasses import replace
from urllib.parse import urlsplit
from uuid import UUID

from .models import API_STYLES, ModelConfig
from .rules import ProviderProfile, RequestRules
from .provider_profiles import load_provider_profiles
from .reasoning import ReasoningProfile


def validate_model_config(config: ModelConfig) -> None:
    """只检查对象和已有快照；提供商目录在创建运行时另行解析。"""
    if not isinstance(config.endpoint_url, str) or not config.endpoint_url.strip():
        raise ValueError("接口地址不能为空。")
    address = urlsplit(config.endpoint_url)
    if address.scheme not in ("http", "https") or not address.hostname:
        raise ValueError("接口地址必须是包含主机名的 http 或 https URL。")
    if not isinstance(config.model, str) or not config.model.strip():
        raise ValueError("模型名称不能为空。")
    if not isinstance(config.api_style, str) or config.api_style not in API_STYLES:
        raise ValueError("接口协议必须是 chat_completions、responses、anthropic_messages 或 gemini。")
    if not isinstance(config.provider_kind, str) or not config.provider_kind.strip():
        raise ValueError("提供商 ID 不能为空。")
    if config.provider_rules is not None:
        if not isinstance(config.provider_rules, RequestRules):
            raise ValueError("provider_rules 须为已解析的请求规则快照。")
        config.provider_rules.validate(config.api_style)
    if config.image_input is not None and type(config.image_input) is not bool:
        raise ValueError("image_input 须为 true、false 或 null（未知能力）。")
    if config.context_window is not None and (type(config.context_window) is not int or config.context_window < 1):
        raise ValueError("context_window 须为正整数或 null（未知容量）。")
    if config.reasoning is not None:
        if not isinstance(config.reasoning, ReasoningProfile):
            raise ValueError("reasoning 须为已解析的模型推理能力。")
        config.reasoning.validate(config.api_style)
    if not isinstance(config.key_env, str) or not config.key_env.strip() or any(
        character.isspace() or character == "=" for character in config.key_env
    ):
        raise ValueError("密钥环境变量名称不能为空，且不能包含空白或等号。")
    if config.credential_ref is not None:
        try:
            if not isinstance(config.credential_ref, str) or UUID(config.credential_ref).hex != config.credential_ref:
                raise ValueError
        except ValueError as exc:
            raise ValueError("credential_ref须为小写UUID或null。") from exc
    if config.api_style == "gemini" and (
        "{model}" not in address.path
        or not address.path.endswith((":generateContent", ":streamGenerateContent"))
    ):
        raise ValueError("Gemini 地址须包含 {model}，并以 :generateContent 或 :streamGenerateContent 结尾。")
    for timeout in (config.timeout_seconds, config.connect_timeout_seconds):
        if (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0
        ):
            raise ValueError("连接和读取超时必须是大于 0 的有限数字，单位为秒。")
    if type(config.max_attempts) is not int or config.max_attempts < 1:
        raise ValueError("联网尝试次数必须是正整数，首次请求计入次数。")


def resolve_model_config(
    config: ModelConfig, *, profiles: dict[str, ProviderProfile] | None = None,
) -> ModelConfig:
    """新运行解析一次规则；已有快照直接复用，循环和恢复不受文件修改影响。"""
    validate_model_config(config)
    if config.provider_rules is not None:
        return config
    profiles = load_provider_profiles() if profiles is None else profiles
    profile = profiles.get(config.provider_kind)
    if profile is None:
        raise ValueError(f"提供商配置中没有 ID {config.provider_kind}。")
    protocol = profile.protocols.get(config.api_style)
    if protocol is None:
        raise ValueError(f"提供商 {config.provider_kind} 未配置协议 {config.api_style}。")
    return replace(config, provider_rules=protocol.request_rules)


def validate_prompt(prompt: str) -> None:
    """拒绝空输入，但保留原文中的换行和空格。"""
    if not prompt.strip():
        raise ValueError("输入内容不能为空。")


def parse_temperature(value: str | float | None) -> float | None:
    """表单文本和命令行数值使用同一约束；留空时不发送温度。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("温度必须是数字，或留空。")
    try:
        temperature = float(value)
    except ValueError as exc:
        raise ValueError("温度必须是数字，或留空。") from exc
    if not math.isfinite(temperature):
        raise ValueError("温度必须是有限数字。")
    # 各服务支持的温度范围不同，这里不强制使用某一家的上下界。
    return temperature


def parse_max_output_tokens(value: str | int | None) -> int | None:
    """最大输出长度是可选的正整数；协议默认值由对应 Provider 决定。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("最大输出 token 必须是正整数，或留空。")
    try:
        limit = int(value)
    except ValueError as exc:
        raise ValueError("最大输出 token 必须是正整数，或留空。") from exc
    if limit <= 0:
        raise ValueError("最大输出 token 必须大于 0。")
    return limit
