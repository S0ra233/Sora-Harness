"""按名排序的有界目录分页；报告总数未知、越界和跨页变化。"""
import asyncio
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...llm.messages import ToolSchema
from ..contracts import ToolContext, ToolDefinition, ToolDisplay, ToolError
from .coverage import Coverage
from .evidence import file_version
from .limits import MAX_ENTRIES, MAX_EXAMINED_ENTRIES, MAX_OUTPUT_CHARS
from .paths import resolve_workspace_path


def create_list_directory_tool() -> ToolDefinition:
    return ToolDefinition(ToolSchema(name="list_directory",
        description=(("列出工作区内指定目录的直接子项（不递归展开子目录）。返回每个条目的名称、类型（文件/目录）、字节大小（`bytes`，目录为 null）、UTC 修改时间（`mtime_utc`）及可能的元数据读取错误（`me"
            "tadata_error`）。结果按确定性顺序排列并支持分页；当目录条目较多返回 `next_offset` 或标记为截断时，可传入 `offset` 与上一页的 `directory_version` 继续读"
            "取下一页。适合在了解项目顶层结构或确认某个具体目录内容时使用；若要在整个项目树中按名字找文件，请优先使用 `find_files`。单次最多200项／20,000条目JSON字符，扫描最多5,000个直接子项；"
            "enumeration_complete=false时总数未知。directory_changed时从offset=0重列。")),
        parameters={"type": "object", "additionalProperties": False, "properties": {
            "path": {"type": "string", "default": "."}, "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ENTRIES, "default": 100},
            "directory_version": {"type": "string"}}}), execute=list_directory,
        display=ToolDisplay("directory", "folder", "列出", "列出了{n}个目录"),
        default_profiles=("desktop_work", "cli_work"))


async def list_directory(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if arguments.keys() - {"path", "offset", "limit", "directory_version"}:
        raise ToolError("invalid_arguments", "仅支持path、offset、limit、directory_version。")
    offset, limit = arguments.get("offset", 0), arguments.get("limit", 100)
    expected = arguments.get("directory_version")
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_ENTRIES or (expected is not None and not isinstance(expected, str)):
        raise ToolError("invalid_arguments", "offset须为非负整数，limit须为1到200；directory_version须为字符串。")
    path = resolve_workspace_path(arguments.get("path", "."), context.workspace, kind="directory")
    coverage, entries = Coverage("filesystem", scanned_files=None, read_bytes=0), []
    try:
        version = file_version(path.stat())
        with os.scandir(path) as directory:
            for index, entry in enumerate(directory):
                if index == MAX_EXAMINED_ENTRIES:
                    coverage.limit_reason = "entry_limit"
                    break
                if index % 64 == 0:
                    await asyncio.sleep(0)
                try:
                    kind = "link" if entry.is_symlink() else "directory" if entry.is_dir(follow_symlinks=False) else "file" if entry.is_file(follow_symlinks=False) else "other"
                except OSError:
                    kind = "unknown"
                    coverage.skip("stat_error")
                entries.append({"name": entry.name, "type": kind})
        changed = version != file_version(path.stat()) or (expected is not None and expected != version)
    except OSError as exc:
        raise ToolError("directory_read_error", "无法枚举目录。", details={"resolved_path": str(path), "operation": "enumerate_directory",
                        "errno": exc.errno, "winerror": getattr(exc, "winerror", None)}) from exc
    entries.sort(key=lambda item: (item["name"].casefold(), item["name"]))
    total = len(entries) if coverage.limit_reason is None and not changed else None
    page, size = [], 2
    for entry in entries[offset:offset + limit]:
        # 只采样候选返回页，避免为整份观察集合重复stat或全文hash。
        row = _entry_metadata(path / entry["name"], entry)
        if row["metadata_error"] is not None:
            coverage.skip("metadata_error")
        cost = len(json.dumps(row, ensure_ascii=False, allow_nan=False)) + (2 if page else 0)
        if size + cost > MAX_OUTPUT_CHARS:
            coverage.output_clipped = True
            break
        page.append(row)
        size += cost
        if len(page) % 32 == 0:
            await asyncio.sleep(0)
    try:
        changed = changed or version != file_version(path.stat())
    except OSError as exc:
        raise ToolError("directory_read_error", "返回前无法复核目录版本。", details={"resolved_path": str(path),
                        "operation": "confirm_directory_version", "errno": exc.errno, "winerror": getattr(exc, "winerror", None)}) from exc
    if changed:
        total = None
    more = offset + len(page) < len(entries)
    return {"path": path.relative_to(context.workspace).as_posix(), "resolved_path": str(path), "entries": page,
            "status": "partial" if coverage.skipped else "complete",
            "total_entries": total, "examined_entries": len(entries), "directory_version": version,
            "directory_changed": changed, "out_of_range": offset >= total and offset > 0 if total is not None else None,
            "ordering": "name_casefold_within_observed_entries", "enumeration_complete": total is not None,
            "has_more": more, "page_clipped": coverage.output_clipped,
            "truncated": more or coverage.limit_reason is not None or changed,
            "next_offset": offset + len(page) if more else None,
            "coverage": coverage.result(complete=not changed and not coverage.skipped)}


def _entry_metadata(path: Path, entry: dict[str, str]) -> dict[str, Any]:
    """采样单项事实；失败不丢失已经枚举到的名字，不沿链接读取目标。"""
    row = {**entry, "bytes": None, "mtime_utc": None, "metadata_error": None}
    operation = "stat_entry_no_follow"
    try:
        value = path.stat(follow_symlinks=False)
        mode = value.st_mode
        row["type"] = ("link" if stat.S_ISLNK(mode) else "directory" if stat.S_ISDIR(mode)
                       else "file" if stat.S_ISREG(mode) else "other")
        row["bytes"] = value.st_size if stat.S_ISREG(mode) else None
        operation = "format_mtime_utc"
        row["mtime_utc"] = datetime.fromtimestamp(value.st_mtime, timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError) as exc:
        row["metadata_error"] = {"code": "metadata_unavailable", "operation": operation,
                                 "errno": getattr(exc, "errno", None), "winerror": getattr(exc, "winerror", None)}
    return row
