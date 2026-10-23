"""按对话保存翻译阅读位置；不进入模型历史，也不复制项目正文。"""
from dataclasses import asdict, dataclass
import json
from pathlib import Path

from ....storage.atomic import write_text_atomic


@dataclass(frozen=True)
class TranslationViewState:
    root: str
    workspace: str | None = None
    scene_id: str | None = None
    seen_calls: tuple[str, ...] = ()


class TranslationViewStore:
    """只在使用时读一次；损坏文件不被默认值覆盖，写入成功后才提交内存。"""

    def __init__(self, path: Path):
        self.path = path
        self.error = None
        self._loaded = False
        self._states = {}

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or type(data.get("version")) is not int
                    or data["version"] != 1 or not isinstance(data.get("conversations"), dict)):
                raise ValueError("需要版本1和对话目录。")
            states = {}
            for identifier, item in data["conversations"].items():
                if not isinstance(identifier, str) or not identifier or not isinstance(item, dict):
                    raise ValueError("对话记录格式不正确。")
                root, workspace = item.get("root"), item.get("workspace")
                scene_id, calls = item.get("scene_id"), item.get("seen_calls", [])
                if (not isinstance(root, str) or not Path(root).is_absolute()
                        or workspace is not None and (not isinstance(workspace, str) or not Path(workspace).is_absolute())
                        or scene_id is not None and (not isinstance(scene_id, str) or not scene_id)
                        or not isinstance(calls, list) or any(not isinstance(call, str) or not call for call in calls)):
                    raise ValueError("项目路径、场景或工具调用记录格式不正确。")
                states[identifier] = TranslationViewState(root, workspace, scene_id, tuple(calls))
            self._states = states
        except FileNotFoundError:
            pass
        except (OSError, ValueError, RecursionError) as exc:
            self.error = f"翻译阅读记录无法读取，原文件保留：{exc}"

    def get(self, conversation_id):
        self._load()
        return self._states.get(conversation_id)

    def save(self, conversation_id: str, state: TranslationViewState):
        self._load()
        if self.error is not None:
            raise ValueError(self.error)
        if self._states.get(conversation_id) == state:
            return
        states = {**self._states, conversation_id: state}
        content = {"version": 1, "conversations": {key: asdict(value) for key, value in states.items()}}
        write_text_atomic(self.path, json.dumps(content, ensure_ascii=False, indent=2) + "\n")
        self._states = states
