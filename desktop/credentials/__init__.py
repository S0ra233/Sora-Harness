"""请求入口共用密钥解析；配置与运行记录只保存非敏感引用。"""
import os

from ..llm.models import ModelConfig
from .windows import read, save


def resolve_api_key(config: ModelConfig) -> str:
    """引用读取失败直接报告；只有旧配置才从指定环境变量读取。"""
    if config.credential_ref is not None:
        return read(config.credential_ref)
    key = os.environ.get(config.key_env, "").strip()
    if not key:
        raise ValueError(f"未配置密钥：请在设置填写密钥，或设置兼容环境变量 {config.key_env}。")
    return key
