"""准备阶段固定搜索后端并执行无密钥的rg版本探针，运行失败不切后端。"""
import math
import os
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

SEARCH_TOOLS = frozenset({"find_files", "search_text"})


@dataclass(frozen=True)
class SearchConfig:
    backend: str = "auto"
    executable: str | None = None
    timeout_seconds: float = 30.0
    selection_mode: str | None = None
    probe: dict | None = None
    degraded_reason: str | None = None

    def __post_init__(self):
        if self.backend not in {"auto", "python", "rg"}:
            raise ValueError("搜索后端须为auto、python或rg。")
        if self.executable is not None and (not isinstance(self.executable, str) or not self.executable.strip()
                or "\x00" in self.executable or not Path(self.executable).is_absolute() or self.backend == "python"):
            raise ValueError("rg路径须为绝对路径，不能与python后端同时设置。")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("搜索超时须为有限正数。")
        if self.selection_mode not in {None, "auto", "fixed"} or (self.probe is not None and not isinstance(self.probe, dict)):
            raise ValueError("搜索选择策略或探针记录无效。")
        if self.probe is not None and type(self.probe.get("available")) is not bool:
            raise ValueError("搜索探针须明确available布尔状态。")
        if self.degraded_reason is not None and not isinstance(self.degraded_reason, str):
            raise ValueError("降级原因须为字符串或null。")


def probe_rg(executable: str | None) -> dict:
    result = {"available": False, "resolved_executable": executable,
              "exists": False, "exit_code": None, "errno": None, "winerror": None}
    try:
        path = Path(executable).resolve() if executable is not None else None
        result.update(resolved_executable=str(path) if path else None, exists=path.is_file() if path else False)
        if not result["exists"]:
            result["reason"] = "program_missing"
            return result
        result["bytes"] = path.stat().st_size
        # 仅保留启动所需环境，探针不继承API密钥或用户自定义令牌。
        allowed = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "COMSPEC", "HOME", "LANG", "LC_ALL"}
        environment = {name: value for name, value in os.environ.items() if name.upper() in allowed}
        response = subprocess.run([str(path), "--no-config", "--version"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1.5, env=environment,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
        result.update(exit_code=response.returncode, stderr=response.stderr.decode("utf-8", errors="replace")[:2000])
        result["available"] = response.returncode == 0 and response.stdout.startswith(b"ripgrep ")
        result["version"] = response.stdout.decode("utf-8", errors="replace")[:512]
        result["reason"] = None if result["available"] else "version_probe_failed"
    except subprocess.TimeoutExpired:
        result["reason"] = "version_probe_timeout"
    except (OSError, ValueError, RuntimeError) as exc:
        result.update(reason="version_probe_failed", errno=getattr(exc, "errno", None), winerror=getattr(exc, "winerror", None))
    return result


def checked_search_config(config: SearchConfig, executable: str | None, *, allow_fallback: bool) -> SearchConfig:
    if config.backend == "python":
        return config
    probe = probe_rg(executable)
    mode = config.selection_mode or ("auto" if allow_fallback else "fixed")
    if probe["available"]:
        return replace(config, backend="rg", executable=probe["resolved_executable"], selection_mode=mode, probe=probe, degraded_reason=None)
    if allow_fallback:
        return replace(config, backend="python", executable=None, selection_mode=mode, probe=probe, degraded_reason=probe["reason"])
    return replace(config, backend="rg", executable=probe["resolved_executable"], selection_mode=mode, probe=probe, degraded_reason=None)


def resolve_search_config(config: SearchConfig) -> SearchConfig:
    if config.backend == "python":
        return config
    executable = config.executable if config.executable is not None else shutil.which("rg")
    return checked_search_config(config, executable, allow_fallback=config.backend == "auto" and config.executable is None)
