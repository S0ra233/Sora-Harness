"""复用现有 Shell／rg 定位方式，补充脚本 Python 与 Git 的路径发现。"""
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..tools.command.shells import ShellConfig, resolve_shell
from ..tools.filesystem.search_config import SearchConfig, resolve_search_config
from .models import PROGRAMS, EnvironmentSnapshot, ProgramStatus


def inspect_program(name: str, executable: str | None) -> ProgramStatus:
    """检查实际文件路径；不运行 --version，不把发现路径宣称为兼容性验证。"""
    if executable is None:
        return ProgramStatus(name, issue=f"未找到 {name} 的程序路径。")
    try:
        path = Path(executable)
        if not path.is_absolute() or not path.is_file():
            raise ValueError("程序路径不是已有的绝对文件路径。")
        # Windows 的 Python 商店执行别名不是已经安装的脚本解释器。
        if name == "python" and path.parent.name.casefold() == "windowsapps":
            raise ValueError("找到的是 Windows 商店执行别名，请指定已安装的 Python。")
        return ProgramStatus(name, str(path.resolve(strict=True)))
    except (OSError, ValueError, RuntimeError) as exc:
        return ProgramStatus(name, issue=str(exc))


def discover_program(name: str, override: str | None = None) -> ProgramStatus:
    if override is not None:
        return inspect_program(name, override)
    try:
        if name in {"powershell", "git_bash"}:
            path = resolve_shell(ShellConfig(kind=name)).executable
        elif name == "rg":
            path = resolve_search_config(SearchConfig(backend="rg")).executable
        elif name == "python":
            candidates = [shutil.which("python"), shutil.which("python3")]
            if not getattr(sys, "frozen", False):
                candidates.append(sys.executable)
            status = inspect_program(name, None)
            for candidate in candidates:
                if candidate is not None:
                    status = inspect_program(name, candidate)
                    if status.issue is None:
                        return status
            return status
        else:
            path = shutil.which(name)
            if name == "git" and path is None:
                # Git Bash 已有定位规则；PATH 没登记 git 时仍可找到同一套安装。
                bash = resolve_shell(ShellConfig(kind="git_bash"))
                root = Path(bash.executable).parent.parent
                path = next((str(candidate) for candidate in (root / "cmd/git.exe", root / "bin/git.exe") if candidate.is_file()), None)
        return inspect_program(name, path)
    except (OSError, ValueError, RuntimeError) as exc:
        return ProgramStatus(name, issue=str(exc))


def detect_environment(overrides: dict[str, str]) -> EnvironmentSnapshot:
    """完整清单只在首次或刷新时建立；时间戳不注入模型的稳定运行资料。"""
    return EnvironmentSnapshot(
        datetime.now(timezone.utc).isoformat(), sys.executable,
        {name: discover_program(name, overrides.get(name)) for name in sorted(PROGRAMS)}, dict(overrides),
    )
