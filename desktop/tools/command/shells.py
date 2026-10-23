"""由应用选择 Shell；模型参数不能指定解释器、环境或权限。"""
import base64
import math
import os
import shutil
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class ShellConfig:
    """命令超时是整次运行上限，与模型请求的网络等待分开。"""

    kind: str = "powershell"
    executable: str | None = None
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if self.kind not in ("powershell", "git_bash"):
            raise ValueError("Shell 须为 powershell 或 git_bash。")
        if self.executable is not None and (
            not isinstance(self.executable, str) or not self.executable.strip()
            or not Path(self.executable).is_absolute()
        ):
            raise ValueError("Shell 可执行文件须为绝对路径。")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)) or (
            not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0
        ):
            raise ValueError("命令超时须为有限的正数。")


def resolve_shell(config: ShellConfig) -> ShellConfig:
    """只在应用开放命令工具时查找；不下载，也不把 WSL 的 bash 当作 Git Bash。"""
    if config.executable is not None:
        candidates = [Path(config.executable)]
    elif config.kind == "powershell":
        candidates = [Path(found) for name in ("pwsh", "powershell") if (found := shutil.which(name))]
    else:
        roots = [Path(value) for name in ("ProgramFiles", "ProgramFiles(x86)") if (value := os.environ.get(name))]
        roots.extend([Path(value) / "Programs" for name in ("LOCALAPPDATA",) if (value := os.environ.get(name))])
        candidates = [root / "Git" / "bin" / "bash.exe" for root in roots]
        if found := shutil.which("git"):
            root = Path(found).parent.parent
            candidates.extend(root / folder / "bash.exe" for folder in ("bin", "usr/bin"))
    for candidate in candidates:
        if candidate.is_file():
            return replace(config, executable=str(candidate.resolve(strict=True)))
    raise ValueError(f"未找到 {config.kind}；请先安装所选 Shell，或通过 --shell-path 指定已有可执行文件。")


def command_argv(config: ShellConfig, command: str) -> list[str]:
    """解释器先等待启动信号，进程树归入管理范围后才执行模型命令。"""
    if config.kind == "git_bash":
        script = "IFS= read -r _harness_start || exit 125\n" + command
        return [config.executable, "--noprofile", "--norc", "-c", script]
    content = base64.b64encode(command.encode("utf-8")).decode("ascii")
    script = (
        "if ([Console]::In.ReadLine() -ne 'start') { exit 125 }\n"
        "$OutputEncoding = [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        "$ErrorActionPreference = 'Stop'\n"
        # 命令作为数据封装，避免语法错误导致 Shell 在归入 Job 前退出。
        "try {\n"
        ". ([scriptblock]::Create([System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + content + "'))))\n"
        "if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }\n"
        "} catch { [Console]::Error.WriteLine($_.ToString()); exit 1 }\n"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [config.executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
