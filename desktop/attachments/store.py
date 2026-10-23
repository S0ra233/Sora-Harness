"""附件接收和持久存储；原图、请求图片与元数据分别保存。"""
import asyncio
import base64
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from ..llm.messages import ImageContent
from ..storage.paths import LocalPaths
from .images import MAX_SOURCE_BYTES, MAX_IMAGE_BYTES, MAX_REQUEST_IMAGES, MAX_REQUEST_BYTES, prepare_image


DEFAULT_DIRECTORY = LocalPaths.default().attachments


@dataclass(frozen=True)
class ImageAttachment:
    attachment_id: str
    name: str
    mime_type: str
    width: int
    height: int
    byte_size: int

    def as_content(self) -> ImageContent:
        return ImageContent(self.attachment_id)


@dataclass(frozen=True)
class EncodedImage:
    mime_type: str
    data: str
    byte_size: int

    @property
    def data_url(self) -> str:
        return f"data:{self.mime_type};base64,{self.data}"


class ImageStore:
    def __init__(self, directory: Path = DEFAULT_DIRECTORY) -> None:
        self.directory = directory.expanduser().resolve()

    def _path(self, attachment_id: str, suffix: str) -> Path:
        ImageContent(attachment_id)
        path = self.directory / f"{attachment_id}{suffix}"
        if not path.resolve().is_relative_to(self.directory):
            raise ValueError("附件文件不能指向存储目录之外。")
        return path

    async def import_files(self, paths: list[Path]) -> list[ImageAttachment]:
        """解码和磁盘 I/O 放在线程中；失败不发布未完成引用，已有文件保留。"""
        return await asyncio.to_thread(self._import_files, paths)

    def _import_files(self, paths: list[Path]) -> list[ImageAttachment]:
        if len(paths) > MAX_REQUEST_IMAGES:
            raise ValueError("一次最多接收 8 张图片。")
        ready = []
        for path in paths:
            with path.expanduser().open("rb") as source_file:
                source = source_file.read(MAX_SOURCE_BYTES + 1)
            try:
                image, width, height = prepare_image(source)
            except ValueError as exc:
                raise ValueError(f"{path.name}：{exc}") from exc
            info = ImageAttachment(uuid4().hex, path.name, "image/png", width, height, len(image))
            ready.append((info, source, image))
        if sum(info.byte_size for info, _, _ in ready) > MAX_REQUEST_BYTES:
            raise ValueError("处理后的图片合计超过 20 MiB，请减少图片。")
        self.directory.mkdir(parents=True, exist_ok=True)
        for info, source, image in ready:
            # 新编号独占创建，绝不覆盖历史附件；元数据最后发布。
            for suffix, content in ((".source", source), (".png", image)):
                with self._path(info.attachment_id, suffix).open("xb") as output:
                    output.write(content)
            with self._path(info.attachment_id, ".json").open("x", encoding="utf-8") as output:
                json.dump(asdict(info), output, ensure_ascii=False, indent=2)
        return [info for info, _, _ in ready]

    def metadata(self, reference: ImageContent) -> ImageAttachment:
        """恢复历史显示时只读取元数据，不把原图复制进会话。"""
        path = self._path(reference.attachment_id, ".json")
        try:
            info = ImageAttachment(**json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"附件 {reference.attachment_id} 的元数据损坏。") from exc
        if info.attachment_id != reference.attachment_id or info.mime_type != "image/png":
            raise ValueError(f"附件 {reference.attachment_id} 的元数据不匹配。")
        if not isinstance(info.name, str) or any(type(value) is not int or value < 1 for value in (info.width, info.height, info.byte_size)):
            raise ValueError(f"附件 {reference.attachment_id} 的名称、尺寸或字节数无效。")
        return info

    def image_path(self, reference: ImageContent) -> Path:
        """本地预览只取得受存储目录约束的请求图，不接受任意用户路径。"""
        return self._path(reference.attachment_id, ".png")

    def encode(self, reference: ImageContent) -> EncodedImage:
        info = self.metadata(reference)
        with self._path(reference.attachment_id, ".png").open("rb") as image_file:
            content = image_file.read(MAX_IMAGE_BYTES + 1)
        if len(content) > MAX_IMAGE_BYTES or len(content) != info.byte_size or not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError(f"附件 {reference.attachment_id} 的请求图片缺失或损坏，请重新选择原图。")
        return EncodedImage(info.mime_type, base64.b64encode(content).decode("ascii"), len(content))
