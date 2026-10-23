"""独立保存界面宽度；不混入模型连接或会话执行配置。"""
import json
from pathlib import Path
from ..storage.atomic import write_text_atomic


class UiPreferencesStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data = {"version": 1, "sidebar_widths": {"conversation": 260, "settings": 260}}
        self.dirty = False
        self.error: str | None = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            widths = data.get("sidebar_widths") if isinstance(data, dict) else None
            if (not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1 or not isinstance(widths, dict)
                    or any(type(widths.get(key, 260)) is not int or not 180 <= widths.get(key, 260) <= 450
                           for key in ("conversation", "settings"))):
                raise ValueError("界面偏好需要版本 1 和 180～450px 的侧栏宽度。")
            self._data = data
        except FileNotFoundError:
            pass
        except (OSError, ValueError, RecursionError) as exc:
            # 损坏文件保留原样，不用默认值自动覆盖它。
            self.error = f"界面偏好未能读取，暂用默认宽度：{exc}"

    def width(self, name: str) -> int:
        return self._data["sidebar_widths"].get(name, 260)

    def set_width(self, name: str, width: int) -> None:
        width = min(450, max(180, width))
        if self.width(name) != width:
            self._data["sidebar_widths"][name] = width
            self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        if self.error is not None:
            raise ValueError(self.error)
        content = json.dumps(self._data, ensure_ascii=False, indent=2) + "\n"
        write_text_atomic(self.path, content)
        self.dirty = False
