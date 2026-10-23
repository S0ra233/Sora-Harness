"""既有提供商入口；默认使用统一适配目录，显式旧路径仍可读取。"""
from pathlib import Path

from .rules import API_STYLES, ProviderProfile, ProviderProtocol, RequestRules, parse_request_rules

DEFAULT_PROVIDERS_PATH = Path(__file__).resolve().parents[1] / "configuration" / "adapters.json"


def load_provider_profiles(path: Path | str = DEFAULT_PROVIDERS_PATH) -> dict[str, ProviderProfile]:
    """延迟导入解析器，数据类型不反向依赖配置存取。"""
    from ..configuration.profiles import load_adapters, load_legacy_provider_profiles
    if Path(path).resolve() == DEFAULT_PROVIDERS_PATH.resolve():
        return load_adapters(path).providers
    return load_legacy_provider_profiles(path)
