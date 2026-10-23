"""环境清单与检查结果；程序路径只表示发现文件，不代表已经执行验证。"""
from dataclasses import dataclass, field

from ..tools.command.shells import ShellConfig
from ..tools.filesystem.search_config import SearchConfig


PROGRAMS = frozenset({"python", "git", "powershell", "git_bash", "rg"})
SCRIPT_PROGRAMS = frozenset({"python", "git"})


@dataclass(frozen=True)
class ProgramStatus:
    name: str
    executable: str | None = None
    issue: str | None = None


@dataclass(frozen=True)
class EnvironmentSnapshot:
    checked_at: str
    application_python: str
    programs: dict[str, ProgramStatus]
    overrides: dict[str, str] = field(default_factory=dict)
    version: int = 1


@dataclass(frozen=True)
class EnvironmentCheck:
    """报告所选能力的实际路径与缺口；缺失项不阻止消息发送。"""

    shell: ShellConfig | None = None
    search: SearchConfig | None = None
    program_paths: dict[str, str | None] = field(default_factory=dict)
    missing: tuple[ProgramStatus, ...] = ()

