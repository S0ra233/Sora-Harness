"""非敏感环境 JSON 存取；损坏配置报错，失败候选文件保留。"""
import json
from dataclasses import asdict
from pathlib import Path
from ..storage.atomic import write_text_atomic

from .models import PROGRAMS, EnvironmentSnapshot, ProgramStatus


def validate_overrides(paths: dict) -> None:
    """用户指定路径是文件边界；这里只校验格式，缺失文件由检测结果报告。"""
    if not isinstance(paths, dict) or paths.keys() - PROGRAMS:
        raise ValueError("环境路径只接受 python、git、powershell、git_bash、rg。")
    if any(not isinstance(path, str) or not path.strip() or not Path(path).is_absolute() for path in paths.values()):
        raise ValueError("指定的程序路径须为非空绝对路径。")


def load_environment(path: Path) -> EnvironmentSnapshot | None:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(content)
        if not isinstance(data, dict) or data.get("version") != 1 or type(data.get("version")) is not int:
            raise ValueError("环境配置版本须为 1。")
        overrides = data.get("overrides", {})
        validate_overrides(overrides)
        programs = data["programs"]
        if not isinstance(programs, dict) or programs.keys() != PROGRAMS:
            raise ValueError("环境清单须包含五种程序。")
        statuses = {}
        for name, value in programs.items():
            status = ProgramStatus(**value)
            if status.name != name or any(item is not None and not isinstance(item, str) for item in (status.executable, status.issue)):
                raise ValueError("环境清单中的程序名称或路径无效。")
            if status.executable is not None and not Path(status.executable).is_absolute():
                raise ValueError("环境清单中的程序路径须为绝对路径。")
            statuses[name] = status
        if not isinstance(data["checked_at"], str) or not isinstance(data["application_python"], str):
            raise ValueError("环境清单的时间与应用 Python 路径须为文本。")
        return EnvironmentSnapshot(data["checked_at"], data["application_python"], statuses, overrides)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ValueError(f"无法读取环境配置 {path}：{exc}") from exc


def save_environment(path: Path, snapshot: EnvironmentSnapshot) -> None:
    content = json.dumps(asdict(snapshot), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    write_text_atomic(path, content)
