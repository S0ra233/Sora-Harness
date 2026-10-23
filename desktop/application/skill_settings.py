"""技能来源设置独立于提示词、模型连接及工具执行权限。"""
import json
from dataclasses import dataclass
from pathlib import Path

from ..storage.atomic import write_text_atomic


@dataclass(frozen=True)
class SkillOptions:
    project_enabled: bool = True
    user_enabled: bool = True
    custom_roots: tuple[str, ...] = ()

    def __post_init__(self):
        if type(self.project_enabled) is not bool or type(self.user_enabled) is not bool:
            raise ValueError("技能来源开关须为布尔值。")
        if not isinstance(self.custom_roots, (tuple, list)) or any(
                not isinstance(value, str) or not value.strip() or not Path(value).expanduser().is_absolute()
                for value in self.custom_roots):
            raise ValueError("自定义技能目录须为绝对路径，每行一项。")
        object.__setattr__(self, "custom_roots", tuple(dict.fromkeys(self.custom_roots)))


class SkillSettings:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> SkillOptions:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return SkillOptions()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
                raise ValueError("技能设置版本不正确。")
            return SkillOptions(**{key: value for key, value in data.items() if key != "version"})
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValueError(f"技能设置读取失败，原文件保留：{exc}") from exc

    def save(self, options: SkillOptions):
        write_text_atomic(self.path, json.dumps({"version": 1, "project_enabled": options.project_enabled,
            "user_enabled": options.user_enabled, "custom_roots": list(options.custom_roots)}, ensure_ascii=False, indent=2))
