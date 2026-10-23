"""有界的本地文字搜索；后端由应用确定，不猜编码，不将局部结果冒充完整扫描。"""
import asyncio
import codecs
import re
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ...llm.messages import ToolSchema
from ..contracts import ToolContext, ToolDefinition, ToolDisplay, ToolError
from .discovery import FilePattern, ScanProgress, candidate_files
from .limits import CHUNK_BYTES, MAX_OUTPUT_CHARS, MAX_SEARCH_READ_BYTES, MAX_SEARCH_READ_FILES
from .paths import resolve_workspace_path
from .ripgrep import search_text_rg
from .search_output import OUTPUT_MODES, SearchSummary, excerpt_size, search_stop_reason


MAX_MATCHES = 50
MAX_READ_FILES = MAX_SEARCH_READ_FILES
MAX_READ_BYTES = MAX_SEARCH_READ_BYTES
MAX_CONTEXT_LINES = 5


@dataclass
class SearchProgress(ScanProgress):
    """在共用遍历计数上增加文本读取量。"""

    read_files: int = 0
    read_bytes: int = 0


def create_search_text_tool() -> ToolDefinition:
    return ToolDefinition(
        ToolSchema(
            name="search_text",
            description=(
                ("在工作区目录或单个文件中按 query 搜索字面文本，不支持正则表达式，默认忽略大小写，支持超大文件流式扫描（无单文件 1 MiB 限制）。你可以通过 output_mode控制返回粒度：\n- 默认正文模式：返"
            "回命中行号、匹配行内容及可选的上下文相邻行（超长单行会自动截取有界窗口并标注 `original_chars` 与 `clipped_chars`）；\n- `files_with_matches`：仅返回包含命"
            "中的文件路径列表，适合大范围初步定位；\n- `count`：返回每个文件的命中行数统计；\n- `summary`：返回整体命中概览。\n建议在搜索高频词时先用 `files_with_matches` 或 `co"
            "unt` 摸底，再针对重点文件用 `read_text_file` 精读。请留意返回中的 `stop_reason`（如达到命中上限、读取预算 8 MiB 或 500 个文件上限）与扫描缺口说明。query为必"
            "填的1～200字符单行字面文字。glob只过滤文件路径：*不跨层，**跨层；不读.gitignore。content默认最多20条、max_matches最多50条，每行最多300字符，正文合计20,000字符"
            "。计数单位为命中行，不是出现次数；count最多显示max_matches个文件，result_truncated只表示计数表未全部显示。实际预算见environment.file_search；rg主动早停e"
            "xit_code为null，原始码留在收据。")
            ),
            parameters={
                "type": "object", "additionalProperties": False,
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "query": {"type": "string", "minLength": 1, "maxLength": 200},
                    "glob": {"type": "string", "minLength": 1, "maxLength": 200, "default": "*"},
                    "max_matches": {"type": "integer", "minimum": 1, "maximum": MAX_MATCHES, "default": 20},
                    "case_sensitive": {"type": "boolean", "default": False},
                    "context_lines": {"type": "integer", "minimum": 0, "maximum": MAX_CONTEXT_LINES, "default": 0},
                    "output_mode": {"type": "string", "enum": list(OUTPUT_MODES), "default": "content"},
                },
                "required": ["query"],
            },
        ),
        execute=search_text,
        display=ToolDisplay("search", "search", "搜索", "搜索了{n}次"),
        default_profiles=("desktop_work", "cli_work"),
    )


def _search_arguments(arguments: dict[str, Any]) -> tuple[str, FilePattern, int, bool, int, str]:
    if arguments.keys() - {"path", "query", "glob", "max_matches", "case_sensitive", "context_lines", "output_mode"}:
        raise ToolError("invalid_arguments", "仅支持path、query、glob、max_matches、case_sensitive、context_lines、output_mode。")
    query, glob = arguments.get("query"), arguments.get("glob", "*")
    count = arguments.get("max_matches", 20)
    if not isinstance(query, str) or not query or len(query) > 200 or any(char in query for char in ("\x00", "\n", "\r")):
        raise ToolError("invalid_arguments", "query 必须是 1 到 200 字符的单行字面文字，不含空字节。")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_MATCHES:
        raise ToolError("invalid_arguments", f"max_matches 必须是 1 到 {MAX_MATCHES} 的整数。")
    case_sensitive, context_lines = arguments.get("case_sensitive", False), arguments.get("context_lines", 0)
    if not isinstance(case_sensitive, bool):
        raise ToolError("invalid_arguments", "case_sensitive 必须是布尔值。")
    if isinstance(context_lines, bool) or not isinstance(context_lines, int) or not 0 <= context_lines <= MAX_CONTEXT_LINES:
        raise ToolError("invalid_arguments", f"context_lines 必须是 0 到 {MAX_CONTEXT_LINES} 的整数。")
    mode = arguments.get("output_mode", "content")
    if not isinstance(mode, str) or mode not in OUTPUT_MODES:
        raise ToolError("invalid_arguments", "output_mode须使用声明中的正文、文件名、计数或汇总模式。")
    if mode != "content" and context_lines:
        raise ToolError("invalid_arguments", "context_lines仅用于content模式。")
    return query, FilePattern(glob), count, case_sensitive, context_lines, mode


@dataclass
class _SearchLine:
    """长行只保留头、尾和首个命中窗口，内存不随行长增长。"""

    pattern: re.Pattern
    length: int = 0
    head: str = ""
    tail: str = ""
    hit: int | None = None
    window: str = ""
    window_start: int = 0

    def feed(self, text: str) -> None:
        combined = self.tail + text
        base = self.length - len(self.tail)
        if self.hit is None:
            found = self.pattern.search(combined)
            if found is not None:
                self.hit = base + found.start()
                self.window_start = max(0, self.hit - 100)
                offset = self.window_start - base
                self.window = combined[offset:offset + 300]
        elif len(self.window) < 300:
            offset = max(0, self.window_start + len(self.window) - self.length)
            self.window += text[offset:offset + 300 - len(self.window)]
        self.head = (self.head + text)[:300]
        self.length += len(text)
        self.tail = combined[-max(300, len(self.pattern.pattern) + 100):]

    def result(self, number: int):
        def window(text, start):
            return {"line": number, "text": text, "snippet_start_column": start + 1,
                    "original_chars": self.length, "clipped_chars": self.length - len(text),
                    "line_truncated": self.length > len(text)}
        nearby = window(self.head, 0)
        if self.hit is None:
            return nearby, None
        start = min(self.window_start, max(0, self.length - 300))
        text = self.window if start == self.window_start else self.tail[start - (self.length - len(self.tail)):][:300]
        return nearby, window(text, start)


async def _search_lines(path: Path, progress: SearchProgress, pattern: re.Pattern):
    """扫描部分也可返回真实命中；耗尽总读取预算时明确保留不完整覆盖。"""
    progress.read_files += 1
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    line, number, pending_cr = _SearchLine(pattern), 1, ""
    try:
        with path.open("rb") as source:
            while True:
                remaining = MAX_READ_BYTES - progress.read_bytes
                # 预算恰好等于文件长度时只探测一字节，区分真正EOF与预算早停。
                raw = source.read(min(CHUNK_BYTES, remaining) if remaining else 1)
                progress.read_bytes += len(raw)
                progress.coverage.read_bytes += len(raw)
                if remaining == 0 and raw:
                    progress.limit_reason = "byte_limit"
                    if line.hit is not None:
                        context_line, hit = line.result(number)
                        for item in (context_line, hit):
                            item.update(line_complete=False, original_chars=None, clipped_chars=None, line_truncated=True)
                        yield context_line, hit
                    return
                if b"\x00" in raw:
                    progress.skipped_paths += 1
                    progress.coverage.skip("binary")
                    return
                text = pending_cr + decoder.decode(raw, final=not raw)
                pending_cr = "\r" if raw and text.endswith("\r") else ""
                if pending_cr:
                    text = text[:-1]
                position = 0
                for match in re.finditer(r"\r\n|\r|\n", text):
                    line.feed(text[position:match.start()])
                    yield line.result(number)
                    line, number = _SearchLine(pattern), number + 1
                    position = match.end()
                    if number % 256 == 0:
                        await asyncio.sleep(0)
                line.feed(text[position:])
                if not raw:
                    if line.length:
                        yield line.result(number)
                    progress.coverage.scanned_files += 1
                    return
                await asyncio.sleep(0)
    except UnicodeError:
        progress.skipped_paths += 1
        progress.coverage.skip("non_utf8")
    except OSError:
        progress.read_errors += 1
        progress.coverage.skip("file_read_error")


async def search_text(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """保留原命中字段；可选前后行只辅助判断，更多文本仍按行读取。"""
    query, glob, count, case_sensitive, nearby, mode = _search_arguments(arguments)
    root = resolve_workspace_path(arguments.get("path", "."), context.workspace, kind="file_or_directory")
    if context.search_config is not None:
        if context.search_config.backend == "rg":
            return await search_text_rg(root, query, glob, count, case_sensitive, nearby, context, output_mode=mode)
        if context.search_config.backend != "python":
            raise ToolError("search_not_configured", "应用尚未确定本轮搜索后端。")
    pattern = re.compile(re.escape(query), 0 if case_sensitive else re.IGNORECASE)
    if mode != "content":
        return await _search_summary(root, query, glob, count, case_sensitive, pattern, mode, context)
    progress = SearchProgress()
    matches, size = [], 0
    # 显式关闭异步生成器，提前停止和取消也立即关闭尚在枚举的目录句柄。
    candidates = candidate_files(root, glob, progress)
    try:
        async for path in candidates:
            await asyncio.sleep(0)
            if progress.read_files == MAX_READ_FILES:
                progress.limit_reason = "file_limit"
                break
            relative = path.relative_to(context.workspace).as_posix()
            before, pending = deque(maxlen=nearby), []
            lines = _search_lines(path, progress, pattern)
            try:
                async for context_line, hit in lines:
                    if progress.limit_reason is not None and progress.limit_reason != "byte_limit":
                        break
                    for item in pending:
                        if size + len(context_line["text"]) > MAX_OUTPUT_CHARS:
                            progress.limit_reason = "output_limit"
                            progress.coverage.output_clipped = True
                            break
                        item["context_after"].append(context_line)
                        size += len(context_line["text"])
                    if progress.limit_reason is not None and progress.limit_reason != "byte_limit":
                        break
                    pending = [item for item in pending if len(item["context_after"]) < nearby]
                    if hit is not None and len(matches) < count:
                        match = {"path": relative, **hit}
                        if nearby:
                            match.update(context_before=list(before), context_after=[])
                        cost = excerpt_size(match)
                        if size + cost > MAX_OUTPUT_CHARS:
                            progress.limit_reason = "output_limit"
                            progress.coverage.output_clipped = True
                            break
                        matches.append(match)
                        size += cost
                        if nearby:
                            pending.append(match)
                    before.append(context_line)
                    if progress.limit_reason is not None:
                        break
                    if len(matches) == count and not pending:
                        progress.limit_reason = "match_limit"
                        break
            finally:
                await lines.aclose()
            if len(matches) == count and progress.limit_reason is None:
                progress.limit_reason = "match_limit"
            if progress.limit_reason is not None:
                break
    finally:
        await candidates.aclose()
    progress.coverage.limit_reason = progress.limit_reason
    parts = [part for item in matches for part in (item, *item.get("context_before", []), *item.get("context_after", []))]
    unknown_clipping = any(part["clipped_chars"] is None for part in parts)
    progress.coverage.clipped_chars = None if unknown_clipping else sum(part["clipped_chars"] for part in parts)
    progress.coverage.output_clipped |= unknown_clipping or bool(progress.coverage.clipped_chars)
    counts = {key: value for key, value in asdict(progress).items() if key != "coverage"}
    return {
        "path": root.relative_to(context.workspace).as_posix(), "query": query, "matches": matches, "backend": "python", "output_mode": mode,
        "case_sensitive": case_sensitive, "context_lines": nearby,
        "truncated": progress.limit_reason is not None or progress.read_errors > 0,
        "stop_reason": search_stop_reason(progress.limit_reason, io_errors=progress.read_errors > 0),
        **counts, "coverage": progress.coverage.result(complete=progress.read_errors == 0 and progress.skipped_paths == 0),
        "files_with_hits": len({item["path"] for item in matches}),
        "coverage_scope": "当前过滤规则下按UTF-8分块扫描的部分；早停及编码／读取错误前的命中仍保留，不证明全文已搜索。",
    }


async def _search_summary(root, query, glob, count, case_sensitive, pattern, mode, context) -> dict:
    """只存每文件计数，不保留命中正文；总计继续受原遍历、字节和文件预算约束。"""
    progress = SearchProgress()
    summary = SearchSummary(mode, count)
    candidates = candidate_files(root, glob, progress)
    try:
        async for path in candidates:
            if progress.read_files == MAX_READ_FILES:
                progress.limit_reason = "file_limit"
                break
            lines, matched = _search_lines(path, progress, pattern), 0
            try:
                async for _, hit in lines:
                    if hit is not None:
                        matched += 1
                    if mode == "files_with_matches" and matched:
                        break
            finally:
                await lines.aclose()
            summary.add_file(path.relative_to(context.workspace).as_posix(), matched)
            if summary.stop_at_file_cap:
                progress.limit_reason = "output_limit" if summary.result_truncated else "match_limit"
            if progress.limit_reason is not None:
                break
            await asyncio.sleep(0)
    finally:
        await candidates.aclose()
    progress.coverage.limit_reason = progress.limit_reason
    counts = {key: value for key, value in asdict(progress).items() if key != "coverage"}
    complete = progress.read_errors == 0 and progress.skipped_paths == 0
    return {"path": root.relative_to(context.workspace).as_posix(), "query": query, "backend": "python",
            "case_sensitive": case_sensitive, "context_lines": 0, **summary.result(), **counts,
            "truncated": progress.limit_reason is not None or not complete,
            "stop_reason": search_stop_reason(progress.limit_reason, io_errors=progress.read_errors > 0),
            "coverage": progress.coverage.result(complete=complete),
            "coverage_scope": "文件名模式只验证首个命中；计数/汇总仅含已扫描有效窗口的命中行，过滤和读取缺口见coverage。"}
