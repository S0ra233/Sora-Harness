"""逐条解析rg记录；仅保留可返回的命中和附近行，不缓存整树输出。"""
import asyncio
import base64
import binascii
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ...contracts import ToolError
from ..coverage import Coverage
from ..limits import MAX_OUTPUT_CHARS
from ..search_output import SearchSummary, excerpt, excerpt_size


def relative_path(value: str, root: Path, workspace: Path, *, directory: bool) -> str:
    base = root if directory else root.parent
    try:
        path = (base / value).resolve()
        path.relative_to(root if directory else root.parent)
        if not directory and path != root:
            raise ValueError("单文件搜索返回其他文件。")
        return path.relative_to(workspace).as_posix()
    except (ValueError, RuntimeError, OSError) as exc:
        raise ToolError("search_output_invalid", "rg返回的路径不在实际搜索范围。") from exc


def _text(value):
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        if isinstance(value.get("bytes"), str):
            try:
                return base64.b64decode(value["bytes"], validate=True).decode("utf-8")
            except binascii.Error as exc:
                raise ToolError("search_output_invalid", "rg的base64字段损坏。") from exc
    raise ToolError("search_output_invalid", "rg文本字段缺失或无效。")


def _record(raw):
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ToolError("search_output_invalid", "rg返回无效JSON记录。") from exc
    if not isinstance(value, dict) or not isinstance(value.get("type"), str) or not isinstance(value.get("data"), dict):
        raise ToolError("search_output_invalid", "rg记录缺少type/data。")
    return value["type"], value["data"]


def _engine_stats(data):
    """两种结果视图共用统计边界，不将未知值补成零。"""
    native = data.get("stats", {})
    if not isinstance(native, dict):
        raise ToolError("search_output_invalid", "rg统计字段无效。")
    return {key: native[key] for key in ("searches", "searches_with_match", "bytes_searched", "matched_lines", "matches")
            if type(native.get(key)) is int and native[key] >= 0}


@dataclass
class _FileLines:
    path: str
    lines: dict = field(default_factory=dict)
    hits: dict = field(default_factory=dict)
    tail: deque = field(default_factory=deque)
    omitted: str | None = None


def _scope(coverage, stats, omitted):
    return {"backend": "rg", "truncated": coverage.limit_reason is not None or omitted > 0,
            "limit_reason": coverage.limit_reason, "directories": None, "examined_entries": None,
            "skipped_paths": None, "read_errors": None, "read_files": None, "read_bytes": None,
            "engine_stats": stats, "skipped_result_files": omitted,
            "coverage": coverage.result(complete=omitted == 0 and not coverage.skipped.get("unfinished_files")),
            "coverage_scope": ("仅当前过滤规则下的候选；不限制文件总大小，扫描仍受时限及输出预算约束。rg未公开被忽略文件总数，null表示未知。"
                               "observed_result_files是已收到的文件end记录数，files_with_hits是返回命中的去重文件数；二者都不是全树扫描总数。")}


async def parse_paths(stream, root, workspace, limit):
    paths, size = [], 0
    coverage = Coverage("rg", scanned_files=None, read_bytes=None, skipped={"ignored": None})
    async for raw in stream.records():
        try:
            path = relative_path(raw.decode("utf-8"), root, workspace, directory=True)
        except UnicodeError as exc:
            raise ToolError("search_output_invalid", "rg文件路径不是UTF-8。") from exc
        if size + len(path) > MAX_OUTPUT_CHARS:
            coverage.limit_reason, coverage.output_clipped = "output_limit", True
            break
        paths.append(path)
        size += len(path)
        if len(paths) == limit:
            coverage.limit_reason = "match_limit"
            break
        if len(paths) % 64 == 0:
            await asyncio.sleep(0)
    return {"paths": paths, **_scope(coverage, None, 0)}


async def parse_matches(stream, root, workspace, limit, nearby, *, directory):
    files, matches, stats, size, omitted, observed, finished = {}, [], None, 0, 0, 0, 0
    coverage = Coverage("rg", scanned_files=None, read_bytes=None, skipped={"ignored": None})
    def finish():
        if files:
            coverage.skipped["unfinished_files"] = len(files)
        return {"matches": matches, "observed_result_files": finished,
                "files_with_hits": len({match["path"] for match in matches}), **_scope(coverage, stats, omitted)}
    async for raw in stream.records():
        observed += 1
        if observed % 256 == 0:
            await asyncio.sleep(0)
        kind, data = _record(raw)
        if kind == "summary":
            stats = _engine_stats(data)
            coverage.read_bytes = stats.get("bytes_searched")
            continue
        if kind not in {"begin", "match", "context", "end"}:
            continue
        try:
            name = _text(data.get("path"))
        except UnicodeError as exc:
            raise ToolError("search_output_invalid", "rg文件路径无法解码。") from exc
        if name not in files:
            files[name] = _FileLines(relative_path(name, root, workspace, directory=directory), tail=deque(maxlen=nearby))
        current = files[name]
        if kind == "begin":
            continue
        if kind == "end":
            files.pop(name)
            finished += 1
            if data.get("binary_offset") is not None:
                current.omitted = "binary"
            if current.omitted:
                omitted += 1
                coverage.skip(current.omitted)
            else:
                for number in sorted(current.hits):
                    match = {"path": current.path, "line": number, **current.lines[number]}
                    if nearby:
                        match["context_before"] = [{"line": n, **current.lines[n]} for n in range(max(1, number - nearby), number) if n in current.lines]
                        match["context_after"] = [{"line": n, **current.lines[n]} for n in range(number + 1, number + nearby + 1) if n in current.lines]
                    cost = excerpt_size(match)
                    if size + cost > MAX_OUTPUT_CHARS:
                        coverage.limit_reason, coverage.output_clipped = "output_limit", True
                        return finish()
                    matches.append(match)
                    size += cost
                    clipped = match["clipped_chars"] + sum(item["clipped_chars"] for key in ("context_before", "context_after") for item in match.get(key, []))
                    coverage.clipped_chars += clipped
                    coverage.output_clipped |= clipped > 0
                    if len(matches) == limit:
                        coverage.limit_reason = "match_limit"
                        return finish()
            continue
        number = data.get("line_number")
        if type(number) is not int or number < 1:
            raise ToolError("search_output_invalid", "rg行号无效。")
        try:
            line = _text(data.get("lines")).removesuffix("\n").removesuffix("\r")
        except UnicodeError:
            current.omitted = "non_utf8"
            continue
        if current.omitted:
            continue
        column = 0
        if kind == "match":
            hits = data.get("submatches")
            start = hits[0].get("start") if isinstance(hits, list) and hits and isinstance(hits[0], dict) else None
            if type(start) is not int or start < 0:
                raise ToolError("search_output_invalid", "rg命中位置无效。")
            try:
                encoded = line.encode("utf-8")
                if start > len(encoded):
                    raise ToolError("search_output_invalid", "rg命中位置超过行长度。")
                column = len(encoded[:start].decode("utf-8"))
            except UnicodeError as exc:
                raise ToolError("search_output_invalid", "rg命中不在UTF-8边界。") from exc
        if number == 1 and line.startswith("\ufeff"):
            line, column = line[1:], max(0, column - 1)
        row = excerpt(line, max(0, column - 100) if kind == "match" else 0)
        if kind == "match" and len(current.hits) < limit:
            for n, previous in current.tail:
                if number - nearby <= n < number:
                    current.lines.setdefault(n, previous)
            current.hits[number], current.lines[number] = column, row
        elif kind == "context" and any(abs(number - n) <= nearby for n in current.hits):
            current.lines[number] = row
        current.tail.append((number, row))
    return finish()


async def parse_summary(stream, root, workspace, limit, mode, *, directory):
    """紧凑模式仍校验JSON、路径及UTF-8命中；只在文件end后接纳其计数。"""
    files, stats, omitted, finished, observed = {}, None, 0, 0, 0
    summary = SearchSummary(mode, limit)
    coverage = Coverage("rg", scanned_files=None, read_bytes=None, skipped={"ignored": None})

    def finish():
        if files:
            coverage.skipped["unfinished_files"] = len(files)
        return {**summary.result(), "observed_result_files": finished, **_scope(coverage, stats, omitted),
                "coverage_scope": "当前过滤范围内已结束文件的有效结果；files_with_hits计已接纳的命中文件，observed_result_files计收到end的文件。"
                                  "文件名模式仅验证首个命中；count表可省略文件，不影响已扫描合计，scan_complete为false时合计不完整。"}

    async for raw in stream.records():
        observed += 1
        if observed % 256 == 0:
            await asyncio.sleep(0)
        kind, data = _record(raw)
        if kind == "summary":
            stats = _engine_stats(data)
            coverage.read_bytes = stats.get("bytes_searched")
            continue
        if kind not in {"begin", "match", "end"}:
            continue
        try:
            name = _text(data.get("path"))
        except UnicodeError as exc:
            raise ToolError("search_output_invalid", "rg文件路径无法解码。") from exc
        if name not in files:
            files[name] = {"path": relative_path(name, root, workspace, directory=directory), "count": 0, "omitted": None}
        current = files[name]
        if kind == "match":
            if type(data.get("line_number")) is not int or data["line_number"] < 1:
                raise ToolError("search_output_invalid", "rg行号无效。")
            try:
                _text(data.get("lines"))
            except UnicodeError:
                current["omitted"] = "non_utf8"
            current["count"] += 1
        elif kind == "end":
            files.pop(name)
            finished += 1
            if data.get("binary_offset") is not None:
                current["omitted"] = "binary"
            if current["omitted"]:
                omitted += 1
                coverage.skip(current["omitted"])
            else:
                summary.add_file(current["path"], current["count"])
                if summary.stop_at_file_cap:
                    coverage.limit_reason = "output_limit" if summary.result_truncated else "match_limit"
                    return finish()
    return finish()
