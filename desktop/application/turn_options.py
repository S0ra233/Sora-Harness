"""按会话保存下一轮选项，不争用正在运行的历史日志。"""
import json
from dataclasses import asdict
from pathlib import Path
from ..storage.atomic import write_text_atomic
from uuid import UUID

from ..runtime.conversations.models import TurnOptions


class TurnOptionsStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.protected: set[str] = set()

    def _path(self, conversation_id: str) -> Path:
        return self.directory / f"{UUID(conversation_id).hex}.json"

    def load(self, conversation_id: str, *, navigation: dict | None = None) -> TurnOptions | None:
        path = self._path(conversation_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return TurnOptions(**navigation["options"]) if navigation and navigation.get("options") else None
        fields = {1: {"reasoning_effort"}, 2: {"reasoning_effort", "model_target"},
                  3: {"reasoning_effort", "model_target", "prompt_config"}}
        if (not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] not in fields
                or data.get("conversation_id") != path.stem or not isinstance(data.get("options"), dict)
                or set(data["options"]) != fields[data["version"]]):
            raise ValueError(f"会话选项文件格式不正确：{path}")
        options = TurnOptions(**data["options"])
        if navigation and navigation.get("options") and data.get("navigation_id") != navigation["operation_id"]:
            return TurnOptions(**navigation["options"])
        return options

    def save(self, conversation_id: str, options: TurnOptions, *, navigation_id: str | None = None) -> None:
        if conversation_id in self.protected:
            raise OSError("下一轮选项原文件无法读取，禁止覆盖；修复后重新打开会话。")
        path = self._path(conversation_id)
        content = json.dumps({"version": 3, "conversation_id": path.stem, "options": asdict(options), "navigation_id": navigation_id},
                             ensure_ascii=False, indent=2) + "\n"
        write_text_atomic(path, content)
