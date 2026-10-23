"""保存未发送的输入；与会话事实分开，空输入也保存而不删除旧文件。"""
import json
from pathlib import Path
from ..storage.atomic import write_text_atomic
from uuid import UUID
from dataclasses import asdict, dataclass, field

from ..llm.messages import ImageContent, content_from_dict


@dataclass
class InputDraft:
    text: str = ""
    images: list[ImageContent] = field(default_factory=list)
    submitted_input_id: str | None = None
    navigation_id: str | None = None
    prelude: list[dict[str, str]] | None = None


class DraftStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.protected: set[str] = set()

    def _path(self, conversation_id: str) -> Path:
        return self.directory / f"{UUID(conversation_id).hex}.json"

    def load(self, conversation_id: str) -> str:
        """保留旧的纯文本读取入口。"""
        return self.load_input(conversation_id).text

    def load_input(self, conversation_id: str) -> InputDraft:
        path = self._path(conversation_id)
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return InputDraft()
        try:
            data = json.loads(content)
            if (not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] not in (1, 2)
                    or data.get("conversation_id") != UUID(conversation_id).hex or not isinstance(data.get("text"), str)):
                raise ValueError("草稿须包含版本 1／2、对应会话编号和文本。")
            raw_images = data.get("images", [])
            if not isinstance(raw_images, list) or any(not isinstance(item, dict) for item in raw_images):
                raise ValueError("草稿图片须为附件引用数组。")
            images = [content_from_dict(item) for item in raw_images]
            if any(not isinstance(item, ImageContent) for item in images):
                raise ValueError("草稿 images 仅接受图片附件引用。")
            submitted = data.get("submitted_input_id")
            if submitted is not None and (not isinstance(submitted, str) or UUID(submitted).hex != submitted):
                raise ValueError("草稿投递标记须为稳定UUID。")
            navigation = data.get("navigation_id")
            if navigation is not None and (not isinstance(navigation, str) or UUID(navigation).hex != navigation):
                raise ValueError("草稿修订标记须为稳定UUID。")
            prelude = data.get("prelude")
            if prelude is not None and (not isinstance(prelude, list) or len(prelude) % 2 or any(
                    not isinstance(item, dict) or item.keys() != {"role", "content"}
                    or item["role"] != ("user" if i % 2 == 0 else "assistant") or not isinstance(item["content"], str)
                    for i, item in enumerate(prelude))):
                raise ValueError("预填草稿须为成对的用户／AI文本，编辑时允许暂时留空。")
            return InputDraft(data["text"], images, submitted, navigation, prelude)
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"无法读取草稿 {path}：{exc}") from exc

    def save(self, conversation_id: str, text: str, images: list[ImageContent] | None = None, *,
             submitted_input_id: str | None = None, navigation_id: str | None = None,
             prelude: list[dict[str, str]] | None = None) -> None:
        if conversation_id in self.protected:
            raise OSError("草稿原文件无法读取，禁止以空内容覆盖；修复后重新打开会话。")
        if not isinstance(text, str) or any(not isinstance(item, ImageContent) for item in (images or [])):
            raise ValueError("草稿须为文本和图片引用。")
        path = self._path(conversation_id)
        if submitted_input_id is not None and (not isinstance(submitted_input_id, str) or UUID(submitted_input_id).hex != submitted_input_id):
            raise ValueError("草稿投递标记须为稳定UUID。")
        content = json.dumps({"version": 2, "conversation_id": path.stem, "text": text,
                              "images": [asdict(item) for item in (images or [])],
                              "submitted_input_id": submitted_input_id, "navigation_id": navigation_id,
                              "prelude": prelude}, ensure_ascii=False, indent=2) + "\n"
        write_text_atomic(path, content)
