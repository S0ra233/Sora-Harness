"""检测所选环境并返回事实；缺失项不阻止发送，工具执行时再报告错误。"""
from dataclasses import replace
from pathlib import Path
from threading import RLock

from ..tools.command.shells import ShellConfig
from ..tools.filesystem.search_config import SearchConfig, checked_search_config
from .discovery import detect_environment, inspect_program
from .models import SCRIPT_PROGRAMS, EnvironmentCheck, EnvironmentSnapshot, ProgramStatus
from .store import load_environment, save_environment, validate_overrides


class EnvironmentService:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        # 多个会话共用清单；首次发现和显式刷新不能互相覆盖用户路径。
        self._lock = RLock()

    def snapshot(self) -> EnvironmentSnapshot:
        with self._lock:
            saved = load_environment(self.path)
            if saved is not None:
                return saved
            return self.refresh()

    def refresh(self, overrides: dict[str, str] | None = None) -> EnvironmentSnapshot:
        """省略覆盖项时保留用户选择；显式传空字典可恢复自动发现。"""
        with self._lock:
            if overrides is None:
                saved = load_environment(self.path)
                overrides = dict(saved.overrides) if saved is not None else {}
            validate_overrides(overrides)
            snapshot = detect_environment(overrides)
            save_environment(self.path, snapshot)
            return snapshot

    def check(
        self, *, shell: ShellConfig | None = None, search: SearchConfig | None = None,
        program_paths: dict[str, str | None] | None = None,
    ) -> EnvironmentCheck:
        """已有路径保持固定；原来没有路径的程序可在刷新清单后重新发现。"""
        requested = dict(program_paths or {})
        if requested.keys() - SCRIPT_PROGRAMS:
            raise ValueError("附加任务环境只接受 python、git。")
        snapshot = self.snapshot()
        missing, paths = [], {}

        def locate(name: str, explicit: str | None) -> ProgramStatus:
            if explicit is not None:
                return inspect_program(name, explicit)
            candidate = snapshot.programs[name]
            return inspect_program(name, candidate.executable) if candidate.executable is not None else candidate

        if shell is not None:
            status = locate(shell.kind, shell.executable)
            if status.executable is None or status.issue is not None:
                missing.append(status)
                shell = replace(shell, executable=None)
            else:
                shell = replace(shell, executable=status.executable)
        if search is not None and search.backend != "python":
            status = locate("rg", search.executable)
            explicit = search.executable is not None or "rg" in snapshot.overrides
            search = checked_search_config(search, status.executable or search.executable,
                                           allow_fallback=search.backend == "auto" and not explicit)
            if search.backend == "rg" and not search.probe.get("available"):
                missing.append(ProgramStatus("rg", search.executable, search.probe.get("reason")))
        for name, executable in requested.items():
            status = locate(name, executable)
            if status.executable is None or status.issue is not None:
                missing.append(status)
                paths[name] = None
            else:
                paths[name] = status.executable
        return EnvironmentCheck(shell, search, paths, tuple(missing))
