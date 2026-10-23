"""按显式协议选择后端，不根据模型名、厂商品牌或 URL 猜测协议。"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from importlib import import_module

import httpx
from ...attachments.store import ImageStore

from ..contracts import ModelBackend
from ..models import ModelConfig
from ..validation import resolve_model_config
from ..protocols import PROTOCOLS


def _create_registered_provider(entry: str, config: ModelConfig, api_key: str, *,
                                client: httpx.AsyncClient | None = None,
                                attachments: ImageStore | None = None) -> ModelBackend:
    """只加载已选协议；导入／构造失败在请求发出前报告，不改换后端。"""
    try:
        module, name = entry.rsplit(":", 1)
        provider = getattr(import_module(module), name)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"协议 {config.api_style} 的实现入口不可用（{type(exc).__name__}）：{entry}") from exc
    return provider(config, api_key, client=client, attachments=attachments)


# 兼容原构造目录的调用方式；不再独立列举一份协议名单。
PROVIDERS = {name: partial(_create_registered_provider, definition.provider_entry)
             for name, definition in PROTOCOLS.items()}


def create_provider(
    config: ModelConfig, api_key: str,
    *, client: httpx.AsyncClient | None = None, attachments: ImageStore | None = None,
) -> ModelBackend:
    """创建后端并保存连接配置，此时不发送 HTTP 请求。"""
    config = resolve_model_config(config)
    return PROVIDERS[config.api_style](config, api_key, client=client, attachments=attachments)


@asynccontextmanager
async def open_model_session(
    config: ModelConfig, api_key: str, *, attachments: ImageStore | None = None,
) -> AsyncIterator[ModelBackend]:
    """运行时才打开连接；整个循环共用客户端，退出或取消时关闭。"""
    config = resolve_model_config(config)
    timeout = httpx.Timeout(config.timeout_seconds, connect=config.connect_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        yield create_provider(config, api_key, client=client, attachments=attachments)
