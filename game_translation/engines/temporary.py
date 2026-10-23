"""Load an explicitly selected, trusted single-file adapter for one project."""
import hashlib
from pathlib import Path
import sys
import types


def describe(path):
    path = Path(path).resolve()
    if path.suffix.lower() != ".py" or not path.is_file():
        raise ValueError("临时适配器必须是已有的 Python 文件")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def load(record):
    path = Path(record["path"])
    source = path.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    if digest != record["sha256"]:
        raise ValueError("临时适配器内容已改变；请保留旧项目，使用新工作目录重新 prepare")
    # Explicit Python execution, not a sandbox. No automatic directory discovery,
    # downloads or imports of neighbouring game scripts. Avoid creating pycache.
    name = "_game_translation_adapter_" + hashlib.sha256(
        str(path).encode("utf-8") + source).hexdigest()
    if name in sys.modules:
        return sys.modules[name]
    module = types.ModuleType(name)
    module.__file__ = str(path)
    sys.modules[name] = module
    try:
        exec(compile(source, str(path), "exec"), module.__dict__)
        required = ("probe", "prepare", "extract", "read", "write",
                    "font_options", "configure_font")
        missing = [key for key in required if not callable(getattr(module, key, None))]
        if missing:
            raise ValueError("临时适配器缺少函数: " + ", ".join(missing))
    except Exception as exc:
        sys.modules.pop(name, None)
        raise ValueError(f"无法加载临时适配器 {path}: {exc}") from exc
    return module
