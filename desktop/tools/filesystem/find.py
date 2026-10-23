"""按路径模式查找文件，不读取文件内容。"""
from dataclasses import asdict
from typing import Any

from ...llm.messages import ToolSchema
from ..contracts import ToolContext, ToolDefinition, ToolDisplay, ToolError
from .discovery import FilePattern, ScanProgress, candidate_files
from .paths import resolve_workspace_path
from .limits import MAX_OUTPUT_CHARS
from .search_output import search_stop_reason
from .ripgrep import find_files_rg


MAX_RESULTS = 200


def create_find_files_tool() -> ToolDefinition:
    """查找工具独立登记；是否向模型开放由应用选择。"""
    return ToolDefinition(
        ToolSchema(
            name="find_files",
            description=(
                ("在工作区内按必填的 pattern 文件名、扩展名或路径模式快速查找文件路径（底层自动使用已配置的 `rg --files` 或 Python 文件扫描器）。返回匹配的文件路径列表、扫描覆盖范围（`covera"
            "ge`）以及停止原因（`stop_reason`）。注意：当结果因达到扫描上限、耗时预算或局部权限错误而提前结束（`partial`）时，“未命中”不代表全树中绝对不存在该文件；请结合 `coverage` 并"
            "缩小 path 或细化 pattern 继续查找；本工具没有分页游标。pattern忽略大小写：不带/匹配各层文件名，带/相对搜索目录；*、?、[]匹配一层，完整段**跨层，不支持花括号或否定模式。limit默"
            "认100、最多200条／20,000路径字符；忽略目录和预算见environment.file_search，不读取.gitignore；rg主动早停exit_code为null。")
            ),
            parameters={
                "type": "object", "additionalProperties": False,
                "properties": {
                    "pattern": {"type": "string", "minLength": 1, "maxLength": 200},
                    "path": {"type": "string", "default": "."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS, "default": 100},
                },
                "required": ["pattern"],
            },
        ),
        execute=find_files,
        display=ToolDisplay("find", "search", "查找", "查找了{n}次"),
        default_profiles=("desktop_work",),
    )


async def find_files(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """验证实际目录后派发后端；Python 遍历提前停止时立即关闭枚举器。"""
    if arguments.keys() - {"pattern", "path", "limit"}:
        raise ToolError("invalid_arguments", "仅支持 pattern、path、limit 参数。")
    pattern = FilePattern(arguments.get("pattern"))
    limit = arguments.get("limit", 100)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
        raise ToolError("invalid_arguments", f"limit 必须是 1 到 {MAX_RESULTS} 的整数。")
    root = resolve_workspace_path(arguments.get("path", "."), context.workspace, kind="directory")
    if context.search_config is not None:
        if context.search_config.backend == "rg":
            return await find_files_rg(root, pattern, limit, context)
        if context.search_config.backend != "python":
            raise ToolError("search_not_configured", "应用尚未确定本轮搜索后端。")
    progress = ScanProgress()
    paths, size = [], 0
    candidates = candidate_files(root, pattern, progress)
    try:
        async for path in candidates:
            relative = path.relative_to(context.workspace).as_posix()
            if size + len(relative) > MAX_OUTPUT_CHARS:
                progress.limit_reason = "output_limit"
                progress.coverage.output_clipped = True
                break
            paths.append(relative)
            size += len(relative)
            if len(paths) == limit:
                progress.limit_reason = "match_limit"
                break
    finally:
        await candidates.aclose()
    progress.coverage.limit_reason = progress.limit_reason
    progress.coverage.scanned_files = None
    counts = {key: value for key, value in asdict(progress).items() if key != "coverage"}
    return {
        "path": root.relative_to(context.workspace).as_posix(), "pattern": pattern.pattern, "paths": paths, "backend": "python",
        "truncated": progress.limit_reason is not None or progress.read_errors > 0,
        "stop_reason": search_stop_reason(progress.limit_reason, io_errors=progress.read_errors > 0),
        **counts, "coverage": progress.coverage.result(complete=progress.read_errors == 0),
    }
