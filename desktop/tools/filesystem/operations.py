"""创建目录、同设备移动或重命名；目标冲突拒绝，不提供删除或覆盖移动。"""
import errno
import os
import re
from typing import Any

from ...llm.messages import ToolSchema
from ..artifacts import ToolArtifacts
from ..contracts import ToolContext, ToolDefinition, ToolDisplay, ToolError
from .evidence import file_evidence, file_version
from .mutation_queue import mutation_queue
from .paths import resolve_new_path, resolve_workspace_path


def create_directory_tool() -> ToolDefinition:
    return ToolDefinition(ToolSchema(name="create_directory", description=(
        ("在授权工作区内创建新目录（父目录必须已有，缺失时逐层创建；已有目标拒绝覆盖）。当你需要组织新模块输出，或建立归档目录（如 `archive/`）来妥善存放不再使用的旧文件时，先调用本工具创建目标目录。")),
        parameters={"type": "object", "additionalProperties": False,
            "properties": {"path": {"type": "string"}}, "required": ["path"]}), execute=create_directory,
        display=ToolDisplay("create_directory", "folder", "创建", "创建了{n}个目录"), default_profiles=("desktop_work",))


def create_move_tool() -> ToolDefinition:
    return ToolDefinition(ToolSchema(name="move_file", description=(
        ("在Windows工作区同一设备内移动或重命名文件与目录。本工具严格遵循冲突保护原则：若目标路径已存在同名文件或目录，操作会拒绝执行而不会静默覆盖；也不执行跨设备的“复制后删除”。由于平台严禁删除任何文件，当你需"
            "要整理、替换或下线旧文件时，请将原文件移动到已授权的归档目录中以完整保留历史数据。实际工具名为move_file；source与destination均为相对路径，目标父目录必须已有，可传expected_ve"
            "rsion。禁止移动工作区根、链接对象或移入自身子目录；失败先核对实际路径。")),
        parameters={"type": "object", "additionalProperties": False, "properties": {
            "source": {"type": "string"}, "destination": {"type": "string"}, "expected_version": {"type": "string"}},
            "required": ["source", "destination"]}), execute=move_file,
        display=ToolDisplay("move", "folder", "移动", "移动了{n}个路径",
                            subject_fields=("source", "destination"), subject_separator=" → "),
        default_profiles=("desktop_work",))


async def create_directory(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if arguments.keys() != {"path"}:
        raise ToolError("invalid_arguments", "仅支持必填path。")
    path = resolve_new_path(arguments["path"], context.workspace)
    # 根锁协调目录移动与子文件编辑；不持有目录读锁，不改变只读访问。
    async with mutation_queue(context.workspace, path):
        path = resolve_new_path(arguments["path"], context.workspace)
        artifacts = ToolArtifacts(context, "create_directory", path=str(path))
        status = "not_applied"
        try:
            path.mkdir()
            status = "succeeded"
            return {"path": path.relative_to(context.workspace).as_posix(), "status": status,
                    **file_evidence(path), **artifacts.references()}
        except FileExistsError as exc:
            raise ToolError("target_exists", "目录创建前目标已存在，未覆盖。") from exc
        finally:
            artifacts.finish_mutation(status)


async def move_file(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if arguments.keys() - {"source", "destination", "expected_version"}:
        raise ToolError("invalid_arguments", "仅支持source、destination、expected_version。")
    expected = arguments.get("expected_version")
    if expected is not None and (not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None):
        raise ToolError("invalid_arguments", "expected_version须来自先前读取结果。")
    source = resolve_workspace_path(arguments.get("source"), context.workspace, kind="file_or_directory")
    target = resolve_new_path(arguments.get("destination"), context.workspace)
    async with mutation_queue(context.workspace, source, target):
        source = resolve_workspace_path(arguments.get("source"), context.workspace, kind="file_or_directory")
        target = resolve_new_path(arguments.get("destination"), context.workspace)
        if source == context.workspace:
            raise ToolError("workspace_root_forbidden", "不能移动工作目录根。")
        if target == source or source in target.parents:
            raise ToolError("invalid_move", "不能移动到自身或其子目录。")
        entry = context.workspace / arguments["source"]
        if entry.is_symlink() or (hasattr(entry, "is_junction") and entry.is_junction()):
            raise ToolError("link_move_forbidden", "不移动链接对象；请明确实际文件路径。")
        before = source.stat()
        if expected is not None and file_version(before) != expected:
            raise ToolError("file_changed", "源已变化，未移动。", details=file_evidence(source, before))
        if before.st_dev != target.parent.stat().st_dev:
            raise ToolError("cross_device_move_forbidden", "跨设备移动会需要复制后删除，本工具拒绝该操作。")
        # Windows rename对已存在目标原生拒绝。POSIX rename会覆盖，不能用先检查假装原子禁覆写。
        if os.name != "nt":
            raise ToolError("exclusive_move_unsupported", "当前平台未实现原子且不覆盖目标的移动。")
        artifacts = ToolArtifacts(context, "move_file", source=str(source), destination=str(target), source_version=file_version(before))
        status = "not_applied"
        try:
            os.rename(source, target)
            status = "succeeded"
            return {"source": source.relative_to(context.workspace).as_posix(),
                    "destination": target.relative_to(context.workspace).as_posix(), "status": status,
                    **file_evidence(target), **artifacts.references()}
        except OSError as exc:
            code = "cross_device_move_forbidden" if exc.errno == errno.EXDEV else "move_failed"
            raise ToolError(code, "移动失败；不会复制后删除，不覆盖目标，请核对实际源与目标。",
                details={"source": str(source), "destination": str(target), "errno": exc.errno, "winerror": getattr(exc, "winerror", None)}) from exc
        finally:
            artifacts.finish_mutation(status)
