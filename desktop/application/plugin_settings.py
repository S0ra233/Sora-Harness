"""插件显式开关；页面显示与模型执行分别核对，不从提示词授予能力。"""
import json
from pathlib import Path
from threading import RLock

from ..storage.atomic import write_text_atomic


class PluginSettings:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()

    def values(self) -> dict[str, bool]:
        with self._lock:
            if not self.path.exists():
                return {}
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("enabled"), dict):
                raise ValueError("插件开关格式不正确，原文件保留。")
            if any(not isinstance(key, str) or not isinstance(value, bool) for key, value in data["enabled"].items()):
                raise ValueError("插件开关须为布尔值。")
            return data["enabled"]

    def enabled(self, identifier: str, *, default: bool = True) -> bool:
        return self.values().get(identifier, default)

    def set_enabled(self, identifier: str, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("插件启用值不正确。")
        with self._lock:
            values = {**self.values(), identifier: enabled}
            write_text_atomic(self.path, json.dumps({"version": 1, "enabled": values}, ensure_ascii=False, indent=2))
