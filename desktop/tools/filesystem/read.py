"""分段读取任意大小的UTF-8文件；字节续接保留原换行，单次返回有界。"""
import asyncio
import codecs
import hashlib
import os
import re
from typing import Any

from ...llm.messages import ToolSchema
from ..contracts import ToolContext, ToolDefinition, ToolDisplay, ToolError
from .evidence import file_evidence, file_version, validate_read_snapshot
from .limits import CHUNK_BYTES, MAX_LINES, MAX_OUTPUT_CHARS
from .paths import resolve_workspace_path


MAX_BATCH_FILES = 32


def _range_properties() -> dict:
    """单文件与批量条目共用参数规则；每次创建独立的声明字典。"""
    return {
        "path": {"type": "string"},
        "start_line": {"type": "integer", "minimum": 1, "default": 1},
        "max_lines": {"type": "integer", "minimum": 1, "maximum": MAX_LINES, "default": MAX_LINES},
        "cursor": {"type": "object", "additionalProperties": False, "properties": {
            "byte_offset": {"type": "integer", "minimum": 0}, "file_version": {"type": "string"}},
            "required": ["byte_offset", "file_version"]},
    }


def create_read_text_tool() -> ToolDefinition:
    return ToolDefinition(ToolSchema(
        name="read_text_file",
        description=(("按行号区间流式读取一个或多个文本文件的内容，无文件总大小上限。支持两种调用方式：\n1. 单文件读取：传入 `path`、`start_line`（从 1 开始）与 `max_lines`（默认 200 行）；\n"
            "2. 批量区间读取（推荐）：在 `files` 参数中一次传入 1～32 个文件及各自的行号区间，减少多轮调用开销。\n单次调用总输出共享 20,000 字符预算；当内容未读完时，工具会返回精确的续读参数（`ne"
            "xt_cursor` 或 `next_files`）；单文件把 next_cursor 作为 cursor，批量把 next_files 作为 files 续读，cursor 不能与 start_line 同传"
            "。批量读取时若个别文件失败，其余已成功区间的结果仍会保留，未完成区间按 next_files 续读。window_complete仅指请求区间已返回，eof才指文件末尾；本工具sha256只覆盖返回字节，全文哈"
            "希须用stat_file(sha256=true)。")),
        parameters={"type": "object", "additionalProperties": False, "properties": {
            **_range_properties(),
            "files": {"type": "array", "minItems": 1, "maxItems": MAX_BATCH_FILES,
                      "items": {"type": "object", "additionalProperties": False,
                                "properties": _range_properties(), "required": ["path"]}}},
            "anyOf": [{"required": ["path"]}, {"required": ["files"]}]}), execute=read_text_file,
        display=ToolDisplay("read", "file", "读取", "执行了{n}次文件读取", "read", "文件内容",
                            result_subject_fields=("label", "path")),
        default_profiles=("desktop_work", "cli_work"))


def _read_arguments(arguments: dict) -> tuple[int, int, dict | None]:
    if arguments.keys() - {"path", "start_line", "max_lines", "cursor"}:
        raise ToolError("invalid_arguments", "仅支持path、start_line、max_lines、cursor。")
    start, count, cursor = arguments.get("start_line", 1), arguments.get("max_lines", MAX_LINES), arguments.get("cursor")
    if type(start) is not int or start < 1 or type(count) is not int or not 1 <= count <= MAX_LINES:
        raise ToolError("invalid_arguments", "start_line须为正整数，max_lines须为1到200的整数。")
    if cursor is not None:
        if (not isinstance(cursor, dict) or cursor.keys() != {"byte_offset", "file_version"}
                or type(cursor["byte_offset"]) is not int or cursor["byte_offset"] < 0
                or not isinstance(cursor["file_version"], str) or re.fullmatch(r"[0-9a-f]{64}", cursor["file_version"]) is None):
            raise ToolError("invalid_arguments", "cursor须使用上次返回的byte_offset和file_version。")
        if "start_line" in arguments:
            raise ToolError("invalid_arguments", "cursor与start_line不能同时指定。")
    return start, count, cursor


async def text_fragments(source):
    """最多暂存一个块和末尾CR；UTF-8跨块字符及CRLF不能被拆成错误的续接位置。"""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    offset, pending_cr = source.tell(), ""
    while True:
        raw = source.read(CHUNK_BYTES)
        text = pending_cr + decoder.decode(raw, final=not raw)
        pending_cr = "\r" if raw and text.endswith("\r") else ""
        if pending_cr:
            text = text[:-1]
        start = 0
        for match in re.finditer(r"\r\n|\r|\n", text):
            piece = text[start:match.end()]
            end = offset + len(piece.encode("utf-8"))
            yield piece, offset, end, True
            offset, start = end, match.end()
        if start < len(text):
            piece = text[start:]
            end = offset + len(piece.encode("utf-8"))
            yield piece, offset, end, False
            offset = end
        if not raw:
            break
        await asyncio.sleep(0)


async def read_text_file(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    """批量仅安排读取与共享预算，路径、编码和版本校验复用原单文件实现。"""
    if "files" not in arguments:
        return await _read_text_range(arguments, context)
    from .read_batch import read_text_batch
    return await read_text_batch(arguments, context)


async def _read_text_range(arguments: dict[str, Any], context: ToolContext, *, max_chars: int = MAX_OUTPUT_CHARS) -> dict[str, Any]:
    start, count, cursor = _read_arguments(arguments)
    path = resolve_workspace_path(arguments.get("path"), context.workspace, kind="file")
    selected, size, completed = [], 0, 0
    line = None if cursor else 1
    first_line, last_line = None, None
    seen, ended = False, False
    before = None
    offset = cursor["byte_offset"] if cursor else 0
    operation = "open_file"
    try:
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            path_before = path.stat()
            version = file_version(path_before)
            if cursor and cursor["file_version"] not in {version, file_version(before)}:
                raise ToolError("file_changed", "文件已变化，旧cursor不可续接；请重新读取。",
                                details={**file_evidence(path, path_before), "expected_version": cursor["file_version"]})
            offset = cursor["byte_offset"] if cursor else 0
            if offset > before.st_size:
                raise ToolError("out_of_range", "读取游标超过文件末尾。", details=file_evidence(path, before))
            source.seek(offset)
            if offset == 0:
                if source.read(3) != b"\xef\xbb\xbf":
                    source.seek(0)
            begin = finish = source.tell()
            operation = "decode_scanned_text"
            fragments = text_fragments(source)
            try:
                async for text, piece_begin, piece_end, newline in fragments:
                    seen, ended = True, newline
                    if "\x00" in text:
                        raise ToolError("not_text_file", "本次读取含空字节，请使用read_bytes查看二进制。",
                                        details={**file_evidence(path, before), "operation": "decode_scanned_text",
                                                 "observed_marker": "nul_byte", "fragment_start_byte": piece_begin,
                                                 "fragment_end_byte": piece_end})
                    if line is not None and line < start:
                        finish = piece_end
                        if newline:
                            line += 1
                        continue
                    if not selected:
                        begin, first_line = piece_begin, line
                    remaining = max_chars - size
                    part = text[:remaining]
                    if len(part) < len(text) and part.endswith("\r") and text[len(part)] == "\n":
                        part = part[:-1]
                    selected.append(part)
                    size += len(part)
                    finish = piece_begin + len(part.encode("utf-8"))
                    last_line = line
                    if len(part) != len(text):
                        ended = False
                        break
                    if newline:
                        completed += 1
                        if line is not None:
                            line += 1
                    if size == max_chars or completed == count:
                        break
            finally:
                await fragments.aclose()
            scanned_bytes = source.tell() - offset
            evidence, metadata_changed = await validate_read_snapshot(source, path, before, path_before)
            version = file_version(evidence)
    except UnicodeError as exc:
        evidence = file_evidence(path, before) if before is not None else {"resolved_path": str(path)}
        decoding = isinstance(exc, UnicodeDecodeError)
        raise ToolError("unsupported_encoding" if operation == "decode_scanned_text" else "path_encoding_error",
                        "本次扫描部分不是有效UTF-8；不猜测编码。" if operation == "decode_scanned_text" else "操作系统无法编码此文件路径。",
                        details={**evidence, "encoding": getattr(exc, "encoding", None), "operation": operation,
                                 "scan_start_byte": offset, "error_offset_scope": "decoder_buffer" if decoding else "text_or_path",
                                 "error_start": getattr(exc, "start", None), "error_end": getattr(exc, "end", None),
                                 "reason": getattr(exc, "reason", type(exc).__name__)}) from exc
    except OSError as exc:
        raise ToolError("file_read_error", "无法读取文件。", details={"resolved_path": str(path), "errno": exc.errno, "winerror": getattr(exc, "winerror", None)}) from exc
    eof = finish >= before.st_size
    content = "".join(selected)
    total = ((line - 1 if ended else line) if seen else 0) if eof and line is not None else None
    if not selected:
        begin = finish
    partial = bool(selected) and not ended and not eof
    return {"path": path.relative_to(context.workspace).as_posix(), **file_evidence(path, evidence), "metadata_changed": metadata_changed,
            "content": content, "start_line": first_line, "end_line": last_line,
            "total_lines": total, "eof": eof, "out_of_range": not cursor and not selected and start > (total or 0) and not (start == 1 and not seen),
            "next_line": line if not eof and not partial else None,
            "next_cursor": {"byte_offset": finish, "file_version": version} if not eof else None,
            "line_partial": partial, "encoding_validation": "scanned_window" if cursor else "scanned_window_and_prefix",
            "window_complete": eof or completed == count, "completed_lines": completed,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "sha256_scope": {"kind": "returned_bytes", "start": begin, "end": finish},
            "read_bytes": scanned_bytes, "truncated": not eof}
