"""首次启动只补建缺失的空接口目录，不迁移或重写用户数据。"""
import json

from .paths import LocalPaths


def initialize_data(paths: LocalPaths) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    document = {"version": 3, "active_connection": None, "active_model": None, "connections": {}}
    try:
        # 排他创建保证另一实例已经写入配置时不会被默认内容覆盖。
        with paths.settings.open("x", encoding="utf-8") as output:
            output.write(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    except FileExistsError:
        pass
