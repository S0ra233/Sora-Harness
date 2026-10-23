"""通用行为设置与模型连接、提示词预设分开保存。"""
from dataclasses import asdict, dataclass
import json
from pathlib import Path

from ..storage.atomic import write_text_atomic
from ..tools.questions import QUESTION_POLICIES


@dataclass(frozen=True)
class GeneralOptions:
    question_frequency: str = "low"

    def __post_init__(self):
        if not isinstance(self.question_frequency, str) or self.question_frequency not in QUESTION_POLICIES:
            raise ValueError("提问频率须为low、medium或high。")


class GeneralSettings:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> GeneralOptions:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return GeneralOptions()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
                raise ValueError("通用设置版本不正确。")
            return GeneralOptions(**{key: value for key, value in data.items() if key != "version"})
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValueError(f"通用设置读取失败，原文件保留：{exc}") from exc

    def save(self, options: GeneralOptions):
        # 先确认原文件可读，不用一次选择覆盖损坏的数据。
        self.load()
        write_text_atomic(self.path, json.dumps({"version": 1, **asdict(options)}, ensure_ascii=False, indent=2))
