"""请求边界检查整个有效历史的图片；每个附件在一次请求内只编码一次。"""
import asyncio

from ...attachments.images import MAX_REQUEST_IMAGES, MAX_REQUEST_BYTES
from ...attachments.store import EncodedImage, ImageStore
from ..messages import ImageContent, ModelMessage
from ..models import ModelConfig, ModelRequest
from .http import ProviderError


def validate_image_input(request: ModelRequest, config: ModelConfig) -> list[ImageContent]:
    """会话提交前也使用此检查，能力错误时尚未清空输入草稿。"""
    references = [part for message in request.messages if isinstance(message, ModelMessage)
                  for part in message.content if isinstance(part, ImageContent)]
    if not references:
        return []
    if config.image_input is not True:
        reason = "不支持图像输入" if config.image_input is False else "图像输入能力尚未确认"
        raise ProviderError(f"当前模型{reason}；本次没有发送请求，也没有去掉历史图片。请核对模型能力后配置。")
    if len(references) > MAX_REQUEST_IMAGES:
        raise ProviderError("有效请求最多包含 8 张图片（含历史）；请另建会话或减少附件。")
    return references


async def prepare_request_images(
    request: ModelRequest, config: ModelConfig, store: ImageStore | None,
) -> dict[str, EncodedImage]:
    references = validate_image_input(request, config)
    if not references:
        return {}
    active_store = store if store is not None else ImageStore()
    unique = {part.attachment_id: part for part in references}

    def encode() -> dict[str, EncodedImage]:
        images = {key: active_store.encode(part) for key, part in unique.items()}
        if sum(images[part.attachment_id].byte_size for part in references) > MAX_REQUEST_BYTES:
            raise ValueError("有效请求内的图片合计超过 20 MiB，请减少附件。")
        return images

    try:
        return await asyncio.to_thread(encode)
    except (OSError, ValueError) as exc:
        raise ProviderError(f"无法准备图片附件：{exc}") from exc
