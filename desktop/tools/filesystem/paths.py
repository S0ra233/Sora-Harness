"""文件工具共用的实际路径检查，不代替操作系统沙箱。"""
from pathlib import Path
import os
import stat
from typing import Any, Literal

from ..contracts import ToolError


def _resolve_relative_path(value: Any, workspace: Path) -> Path:
    """只接受目录内的相对路径，解析符号链接和目录联接后再核对范围。"""
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ToolError("invalid_arguments", "path 必须是非空的相对路径。")
    relative = Path(value)
    # Windows 盘符相对路径与 NTFS 备用数据流不属于普通文件入口。
    if relative.is_absolute() or relative.drive or any(":" in part for part in relative.parts):
        reason = "absolute_path" if relative.is_absolute() else "drive_relative_path" if relative.drive else "alternate_stream"
        raise ToolError("path_not_allowed", "path 只接受工作目录内的相对路径。",
                        details={"path": value, "workspace": str(workspace), "reason": reason,
                                 "required_path_form": "workspace_relative"})
    try:
        resolved = (workspace / relative).resolve()
    except (ValueError, RuntimeError, OSError) as exc:
        raise ToolError("path_resolution_failed", "路径无法解析。", details={"path": value, "workspace": str(workspace),
                        "operation": "resolve_path", "errno": getattr(exc, "errno", None),
                        "winerror": getattr(exc, "winerror", None)}) from exc
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ToolError("outside_workspace", "实际路径在工作目录外。",
                        details={"path": value, "workspace": str(workspace), "resolved_path": str(resolved)}) from exc
    return resolved


def resolve_workspace_path(
    value: Any, workspace: Path, *, kind: Literal["file", "directory", "file_or_directory"],
) -> Path:
    """读取、列目录和修改复用同一个实际路径边界。"""
    resolved = _resolve_relative_path(value, workspace)
    try:
        mode = resolved.stat().st_mode
    except FileNotFoundError as exc:
        raise ToolError("not_found", "指定路径不存在。", details={"resolved_path": str(resolved)}) from exc
    except NotADirectoryError as exc:
        raise ToolError("not_a_directory", "路径中的父项不是目录。", details={"resolved_path": str(resolved)}) from exc
    except OSError as exc:
        raise ToolError("path_access_denied" if isinstance(exc, PermissionError) else "path_stat_failed", "无法取得路径元数据。",
                        details={"resolved_path": str(resolved), "errno": exc.errno, "winerror": getattr(exc, "winerror", None)}) from exc
    if kind == "file" and not stat.S_ISREG(mode):
        raise ToolError("not_a_file", "路径存在，但不是普通文件。", details={"resolved_path": str(resolved)})
    if kind == "directory" and not stat.S_ISDIR(mode):
        raise ToolError("not_a_directory", "路径存在，但不是目录。", details={"resolved_path": str(resolved)})
    if kind == "file_or_directory" and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise ToolError("unsupported_file_type", "路径不是普通文件或目录。", details={"resolved_path": str(resolved)})
    return resolved


def resolve_new_file_path(value: Any, workspace: Path) -> Path:
    """新文件要求父目录已有；是否重名最终由独占创建再次判断。"""
    try:
        return resolve_new_path(value, workspace)
    except ToolError as exc:
        if exc.code == "target_exists":
            raise ToolError("file_exists", "目标已存在；创建工具不覆盖，请先读取。", details=exc.details) from exc
        raise


def resolve_new_path(value: Any, workspace: Path) -> Path:
    """目录创建和移动的目标：父目录已有且任何同名对象均视为冲突。"""
    resolved = _resolve_relative_path(value, workspace)
    if resolved == workspace:
        raise ToolError("workspace_root_forbidden", "不能用工作目录根作为新目标。")
    if os.path.lexists(resolved) or os.path.lexists(workspace / value):
        raise ToolError("target_exists", "目标已存在，不覆盖文件、目录或链接。", details={"resolved_path": str(resolved)})
    resolve_workspace_path(resolved.parent.relative_to(workspace).as_posix(), workspace, kind="directory")
    return resolved
