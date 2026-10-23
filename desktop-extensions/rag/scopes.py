"""资料范围归插件保存；模型参数只能缩小已选范围。"""
import json
import os
from pathlib import Path
from threading import RLock
from desktop.storage.atomic import write_text_atomic

_LOCK = RLock()


class Scopes:
    def __init__(self, directory):
        self.path = Path(directory) / "scopes.json"

    @staticmethod
    def project(workspace):
        return os.path.normcase(str(Path(workspace).resolve())) if workspace else None

    def load(self):
        if not self.path.exists():
            return {"version": 1, "projects": {}, "conversations": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("资料范围配置损坏，未扩大检索范围。")
        for group in ("projects", "conversations"):
            if not isinstance(data.get(group), dict) or any(not isinstance(key, str) or not isinstance(ids, list)
                or len(ids) > 1000 or any(not isinstance(value, str) for value in ids) for key, ids in data[group].items()):
                raise ValueError("资料范围配置损坏，未扩大检索范围。")
        return data

    def allowed(self, workspace=None, conversation_id=None):
        with _LOCK:
            data = self.load()
        project = data["projects"].get(self.project(workspace))
        conversation = data["conversations"].get(conversation_id)
        if project is None:
            return conversation
        return project if conversation is None else sorted(set(project) & set(conversation))

    def save(self, group, key, identifiers):
        if group not in {"projects", "conversations"} or not key:
            raise ValueError("当前页面没有可绑定的项目或会话。")
        with _LOCK:
            data = self.load()
            if identifiers is None:
                data[group].pop(key, None)
            else:
                data[group][key] = sorted(set(identifiers))
            write_text_atomic(self.path, json.dumps(data, ensure_ascii=False, indent=2))
