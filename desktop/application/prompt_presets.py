"""提示词预设独立于连接和权限；成功写入后才发布新默认。"""
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID, uuid4

from ..prompts import PromptConfig, load_prompt_config
from ..prompts.config import MAX_CONFIG_BYTES
from ..storage.atomic import write_text_atomic


DEFAULT_ID = "00000000000000000000000000000001"


@dataclass(frozen=True)
class PromptPreset:
    preset_id: str
    name: str
    config: PromptConfig


class PromptPresets:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> tuple[tuple[PromptPreset, ...], str]:
        try:
            return self._load()
        except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
            raise ValueError(f"无法读取提示词预设 {self.path}，原文件保留：{exc}") from exc

    def _load(self) -> tuple[tuple[PromptPreset, ...], str]:
        default = PromptPreset(DEFAULT_ID, "默认", load_prompt_config())
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return (default,), DEFAULT_ID
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1 or not isinstance(data.get("presets"), list):
            raise ValueError("提示词预设文件格式无效，原文件保留。")
        presets, ids = [default], set()
        for raw in data["presets"]:
            identifier, name = raw["preset_id"], raw["name"]
            if UUID(identifier).hex != identifier or identifier in ids or not isinstance(name, str) or not name.strip():
                raise ValueError("预设编号或名称无效。")
            config = PromptConfig(**raw["config"])
            self._validate_size(config)
            preset = PromptPreset(identifier, name, config)
            if identifier == DEFAULT_ID:
                presets[0] = preset
            else:
                presets.append(preset)
            ids.add(identifier)
        selected = data["selected_id"]
        if selected not in {preset.preset_id for preset in presets}:
            raise ValueError("默认提示词预设不存在。")
        return tuple(presets), selected

    @staticmethod
    def _validate_size(config: PromptConfig) -> None:
        if len(json.dumps(asdict(config), ensure_ascii=False).encode("utf-8")) > MAX_CONFIG_BYTES:
            raise ValueError("单个提示词配置超过 64 KiB。")

    def selected(self) -> PromptConfig:
        return self.selected_preset().config

    def selected_preset(self) -> PromptPreset:
        """创建会话时同时固定名称和内容，后续设置变化不改旧快照。"""
        presets, identifier = self.load()
        return next(preset for preset in presets if preset.preset_id == identifier)

    def _write(self, presets, selected: str) -> None:
        default = PromptPreset(DEFAULT_ID, "默认", load_prompt_config())
        # 仅把修改后的默认条目存到本地；未修改或恢复后继续使用内置文件。
        document = {"version": 1, "selected_id": selected,
                    "presets": [asdict(preset) for preset in presets if preset != default]}
        write_text_atomic(self.path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")

    def save(self, name: str, config: PromptConfig, identifier: str | None = None) -> PromptPreset:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("预设名称不能为空。")
        self._validate_size(config)
        presets, _ = self.load()
        if identifier is not None and not any(preset.preset_id == identifier for preset in presets):
            raise ValueError("原预设不再存在，未覆盖文件。")
        identifier = uuid4().hex if identifier is None else identifier
        saved = PromptPreset(identifier, name.strip(), config)
        updated = [saved if preset.preset_id == identifier else preset for preset in presets]
        if not any(preset.preset_id == identifier for preset in presets):
            updated.append(saved)
        self._write(updated, identifier)
        return saved

    def delete(self, identifier: str) -> tuple[tuple[PromptPreset, ...], str]:
        """移除所选条目；默认条目恢复内置内容，不删除配置文件或旧会话。"""
        presets, selected = self.load()
        if not any(preset.preset_id == identifier for preset in presets):
            raise ValueError("所选提示词预设不存在，原文件保留。")
        if identifier == DEFAULT_ID:
            default = PromptPreset(DEFAULT_ID, "默认", load_prompt_config())
            updated = tuple(default if preset.preset_id == DEFAULT_ID else preset for preset in presets)
        else:
            updated = tuple(preset for preset in presets if preset.preset_id != identifier)
            if selected == identifier:
                selected = DEFAULT_ID
        self._write(updated, selected)
        return updated, selected

    def select(self, identifier: str) -> None:
        presets, _ = self.load()
        if not any(preset.preset_id == identifier for preset in presets):
            raise ValueError("所选提示词预设不存在。")
        self._write(presets, identifier)
