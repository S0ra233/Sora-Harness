"""两个模型搜索工具共用的 rg 后端；声明和参数仍由工具入口负责。"""
from pathlib import Path
from typing import Any

from ...contracts import ToolContext
from ..discovery import FilePattern, SKIP_DIRECTORIES
from .process import run_rg
from .results import parse_matches, parse_paths, parse_summary


def _scan_arguments(pattern: FilePattern | None) -> list[str]:
    """保持已有忽略策略；正向模式放在固定排除规则前，不能重新包含依赖目录。"""
    arguments = ["--no-ignore", "--hidden", "--color=never"]
    if pattern is not None:
        arguments.append("--iglob=" + pattern.pattern)
    for name in sorted(SKIP_DIRECTORIES):
        arguments.extend(("--iglob=!**/" + name, "--iglob=!**/" + name + "/**"))
    return arguments


async def find_files_rg(root: Path, pattern: FilePattern, limit: int, context: ToolContext) -> dict[str, Any]:
    arguments = ["--files", "--null", *_scan_arguments(pattern), "--", "."]
    result = await run_rg(arguments, context, root, lambda stream: parse_paths(stream, root, context.workspace, limit), separator=b"\x00")
    return {"path": root.relative_to(context.workspace).as_posix(), "pattern": pattern.pattern, **result}


async def search_text_rg(root: Path, query: str, pattern: FilePattern, limit: int, case_sensitive: bool,
                         nearby: int, context: ToolContext, *, output_mode: str = "content") -> dict[str, Any]:
    directory = root.is_dir()
    arguments = ["--json", "--fixed-strings", "--encoding=none", "--no-mmap", "--context=" + str(nearby),
                 *_scan_arguments(pattern if directory else None), "--regexp=" + query]
    if output_mode == "content":
        arguments.append("--max-count=" + str(limit))
    elif output_mode == "files_with_matches":
        arguments.append("--max-count=1")
    if not case_sensitive:
        arguments.append("--ignore-case")
    arguments.extend(("--", "." if directory else root.name))
    consumer = (lambda stream: parse_matches(stream, root, context.workspace, limit, nearby, directory=directory)) if output_mode == "content" else (
        lambda stream: parse_summary(stream, root, context.workspace, limit, output_mode, directory=directory))
    result = await run_rg(arguments, context, root if directory else root.parent, consumer)
    return {
        "path": root.relative_to(context.workspace).as_posix(), "query": query,
        "case_sensitive": case_sensitive, "context_lines": nearby, "output_mode": output_mode, "read_files": None, "read_bytes": None,
        **result,
    }
