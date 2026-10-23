"""按请求顺序读取多个原文区间，共用正文预算并给出精确续读参数。"""
import asyncio

from ..contracts import ToolError
from .limits import MAX_LINES, MAX_OUTPUT_CHARS
from .read import MAX_BATCH_FILES, _read_arguments, _read_text_range


def _batch_arguments(arguments: dict) -> list[dict]:
    if arguments.keys() != {"files"}:
        raise ToolError("invalid_arguments", "批量读取仅传files，路径、起始行、行数和游标填写在各条目内。")
    entries = arguments["files"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_BATCH_FILES:
        raise ToolError("invalid_arguments", "files须包含1～32个文件区间。")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"].strip():
            raise ToolError("invalid_arguments", "每个files条目须包含非空path。", details={"entry_index": index})
        try:
            _read_arguments(entry)
        except ToolError as exc:
            raise ToolError(exc.code, str(exc), details={"entry_index": index}) from exc
    return entries


async def read_text_batch(arguments: dict, context) -> dict:
    """条目读取失败不丢弃邻居结果；取消仍走原工具取消边界，保留已读取进度。"""
    entries = _batch_arguments(arguments)
    results, pending, used, failures = [], list(entries), 0, 0
    progress = {"entries": results, "next_files": pending}
    if context.execution_details is not None:
        context.execution_details["batch_read_progress"] = progress
    for index, entry in enumerate(entries):
        if used >= MAX_OUTPUT_CHARS:
            break
        await asyncio.sleep(0)
        try:
            data = await _read_text_range(entry, context, max_chars=MAX_OUTPUT_CHARS - used)
        except ToolError as exc:
            failures += 1
            results.append({"path": entry["path"], "status": "error",
                            "error": {"code": exc.code, "message": str(exc), "details": exc.details or {}}})
            pending = entries[index + 1:]
        else:
            used += len(data["content"])
            results.append({"status": "ok", "data": data})
            pending = entries[index + 1:]
            if not data["window_complete"] and data["next_cursor"] is not None:
                remaining = entry.get("max_lines", MAX_LINES) - data["completed_lines"]
                pending = [{"path": entry["path"], "cursor": data["next_cursor"], "max_lines": remaining}, *pending]
                progress["next_files"] = pending
                break
        progress["next_files"] = pending
    result = {"label": f"{len(results) - failures}个文件区间", "status": "partial" if pending or failures else "complete",
              "entries": results, "requested_files": len(entries), "processed_files": len(results),
              "succeeded_files": len(results) - failures, "failed_files": failures,
              "batch_complete": not pending, "output_characters": used, "output_character_limit": MAX_OUTPUT_CHARS,
              "next_files": pending, "truncated": bool(pending), "stop_reason": "budget_reached" if pending else "completed"}
    if failures == len(entries):
        raise ToolError("read_batch_failed", "本批所有文件区间读取失败，按各条目的错误修正后再读。", details=result)
    return result
