"""流式快照、精确唯一替换与原子写回；文件总大小不受单次参数预算限制。"""
import asyncio
import codecs
import difflib
import hashlib
import os
import re
from pathlib import Path
from typing import Any

from ...llm.messages import ToolSchema
from ..artifacts import ToolArtifacts
from ..contracts import ToolContext, ToolDefinition, ToolDisplay, ToolError
from .evidence import file_evidence, file_version, hash_file, stat_details
from .limits import CHUNK_BYTES, MAX_EDIT_ARGUMENT_BYTES, MAX_OUTPUT_CHARS, MAX_DIFF_WINDOW_CHARS
from .mutation_queue import mutation_queue
from .paths import resolve_new_file_path, resolve_workspace_path
from .read import text_fragments

# 文件可任意大；局部差异算法只处理有界窗口，超出时明确不提供完整行数。
MAX_DIFF_WINDOW = MAX_DIFF_WINDOW_CHARS


def create_file_tool() -> ToolDefinition:
    return ToolDefinition(ToolSchema(name="create_text_file",
        description=("在工作区内创建一个全新的文本文件并写入内容（单次文本参数上限 8 MiB）。为保护用户数据，如果目标路径已存在文件，本工具会明确拒绝覆盖并返回错误。若要修改已有文件，请先用 `read_text_file` 读"
            "取目标内容，再使用 `edit_text_file` 进行修改。父目录必须已有；返回副本、差异统计和收据。"),
        parameters={"type": "object", "additionalProperties": False,
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"]}), execute=create_text_file,
        display=ToolDisplay("create", "new", "创建", "创建了{n}个文件", "diff", "文件差异"),
        default_profiles=("desktop_work",))


def create_edit_tool() -> ToolDefinition:
    return ToolDefinition(ToolSchema(name="edit_text_file", description=(
        ("通过唯一精确匹配替换（`old_text` → `new_text`）修改工作区内已有的文本文件，采用流式处理，无文件总大小上限（old_text与new_text各最多8 MiB）。调用前请务必先用 `rea"
            "d_text_file` 确认目标区间的最新原文：\n- `old_text` 必须在文件内精确出现且仅出现 1 次（建议包含前后 2～5 行无变化的上下文行以确保唯一性）；\n- 工具会自动保留原文件的 BOM "
            "头与未修改区域的换行符风格（CRLF/LF），并在写入替换前核对文件版本；\n- 严禁使用本工具将非空文件清空或替换为纯空白字符；\n- 若返回 `file_changed` 错误，说明文件版本检查未通过，请先重新"
            "读取最新内容再发起编辑。可传先前读取的expected_version；兼容LF／CRLF，不做模糊匹配；重复匹配、外部变化或取消均不写回，差异统计未知时明确标记。")),
        parameters={"type": "object", "additionalProperties": False, "properties": {
            "path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
            "expected_version": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}), execute=edit_text_file,
        display=ToolDisplay("edit", "new", "编辑", "编辑了{n}个文件", "diff", "文件差异"),
        default_profiles=("desktop_work",))


def _text_argument(arguments, name):
    value = arguments.get(name)
    if not isinstance(value, str) or "\x00" in value:
        raise ToolError("invalid_arguments", f"{name}必须是无空字节的文本。")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise ToolError("invalid_arguments", f"{name}不能编码为UTF-8。") from exc
    if size > MAX_EDIT_ARGUMENT_BYTES:
        raise ToolError("argument_too_large", f"{name}超过单次8 MiB参数预算；这不是文件总大小限制。")
    return value


def _lf(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _raw_offset(text, normalized_offset):
    """LF位置还原到原字符位置，不为文件每个字符建立索引数组。"""
    extra = 0
    for match in re.finditer("\r\n", text):
        if match.start() - extra >= normalized_offset:
            break
        extra += 1
    return normalized_offset + extra


async def _snapshot(path, target):
    digest = hashlib.sha256()
    with path.open("rb") as source, target.open("xb") as output:
        handle_before, before = os.fstat(source.fileno()), path.stat()
        while chunk := source.read(CHUNK_BYTES):
            output.write(chunk)
            digest.update(chunk)
            await asyncio.sleep(0)
        output.flush()
        os.fsync(output.fileno())
        version = file_version(before)
        handle_after, after = os.fstat(source.fileno()), path.stat()
        if file_version(handle_before) != file_version(handle_after) or file_version(after) != version:
            raise ToolError("file_changed", "文件在保存快照期间变化，未修改目标。",
                            details={"handle_before": stat_details(handle_before), "handle_after": stat_details(handle_after),
                                     "path_before": stat_details(before), "path_after": stat_details(after)})
    return before, digest.hexdigest()


async def _locate(snapshot, old):
    """每次保留可能跨块匹配的后缀；完整扫描检测重复，而不是找到首处就写回。"""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    found, newline, buffer, pending_cr = None, None, "", ""
    with snapshot.open("rb") as source:
        bom = source.read(3) == b"\xef\xbb\xbf"
        if not bom:
            source.seek(0)
        byte_base, line_base = source.tell(), 1
        while True:
            raw = source.read(CHUNK_BYTES)
            text = pending_cr + decoder.decode(raw, final=not raw)
            pending_cr = "\r" if raw and text.endswith("\r") else ""
            if pending_cr:
                text = text[:-1]
            if "\x00" in text:
                raise ToolError("not_text_file", "文件包含空字节，不能作为普通文本编辑。")
            if newline is None and (match := re.search(r"\r\n|\n|\r", text)):
                newline = match.group()
            buffer += text
            normalized = _lf(buffer)
            safe = max(0, len(normalized) - len(old) + 1) if raw else len(normalized)
            start = normalized.find(old)
            while start >= 0 and start < safe:
                if found is not None:
                    raise ToolError("ambiguous_text", "old_text匹配不止一处；增加周围文本后重试。")
                begin = _raw_offset(buffer, start)
                end = _raw_offset(buffer, start + len(old))
                found = (byte_base + len(buffer[:begin].encode("utf-8")),
                         byte_base + len(buffer[:end].encode("utf-8")),
                         line_base + normalized.count("\n", 0, start))
                start = normalized.find(old, start + 1)
            flush = _raw_offset(buffer, safe)
            byte_base += len(buffer[:flush].encode("utf-8"))
            line_base += normalized.count("\n", 0, safe)
            buffer = buffer[flush:]
            if not raw:
                break
            await asyncio.sleep(0)
    if found is None:
        raise ToolError("text_not_found", "未找到完全相同的old_text；重新读取后再决定替换。")
    return (*found, newline or "\n")


async def _copy_part(source, output, digest, count=None):
    """按字节复制，未修改部分从快照原样经过，不重新序列化整文件。"""
    while count is None or count > 0:
        chunk = source.read(CHUNK_BYTES if count is None else min(CHUNK_BYTES, count))
        if not chunk:
            if count is not None and count:
                raise ToolError("snapshot_incomplete", "修订快照长度不符，未写回。")
            break
        output.write(chunk)
        digest.update(chunk)
        if count is not None:
            count -= len(chunk)
        await asyncio.sleep(0)


async def _compose(snapshot, candidate, begin, end, replacement):
    digest = hashlib.sha256()
    with snapshot.open("rb") as source, candidate.open("xb") as output:
        await _copy_part(source, output, digest, begin)
        output.write(replacement)
        digest.update(replacement)
        source.seek(end)
        await _copy_part(source, output, digest)
        output.flush()
        os.fsync(output.fileno())
    # 允许删除一段内容，但不能用替换操作规避文件保留规则。
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    nonblank = False
    with candidate.open("rb") as source:
        while raw := source.read(CHUNK_BYTES):
            if decoder.decode(raw).strip():
                nonblank = True
                break
            await asyncio.sleep(0)
    if not nonblank:
        raise ToolError("empty_file_forbidden", "禁止把已有文件改为空或纯空白；可创建归档目录并移动。")
    return digest.hexdigest()


def _diff(before, after, name, line=1):
    """只比较真正受影响的完整行；位置修正到原文件，预览截断不改变计数。"""
    added = removed = 0
    pieces = []
    lines = difflib.unified_diff(_lf(before).splitlines(keepends=True), _lf(after).splitlines(keepends=True),
        fromfile=f"a/{name}", tofile=f"b/{name}")
    in_hunk = False
    for text in lines:
        if text.startswith("@@"):
            in_hunk = True
            text = re.sub(r"([+-])(\d+)", lambda m: m[1] + str(int(m[2]) + line - 1), text)
        elif in_hunk:
            added += text.startswith("+")
            removed += text.startswith("-")
        pieces.append(text if text.endswith("\n") else text + "\n\\ No newline at end of file\n")
    return "".join(pieces), {"added_lines": added, "removed_lines": removed, "complete": True}


async def _change_diff(snapshot, begin, end, old, new, name):
    """局部完整行窗口有独立预算；巨行不能冒充完整Git差异。"""
    pieces, length, line, first_line, active = [], 0, 1, None, False
    current, current_size = [], 0
    with snapshot.open("rb") as source:
        if source.read(3) != b"\xef\xbb\xbf":
            source.seek(0)
        fragments = text_fragments(source)
        try:
            async for text, piece_begin, piece_end, newline in fragments:
                current_size += len(text)
                if current_size <= MAX_DIFF_WINDOW:
                    current.append(text)
                if piece_begin < end and piece_end > begin:
                    active = True
                    first_line = line if first_line is None else first_line
                if active and length + current_size > MAX_DIFF_WINDOW:
                    break
                if newline:
                    if active:
                        pieces.extend(current)
                        length += current_size
                        if piece_end >= end:
                            window = "".join(pieces)
                            normalized = _lf(window)
                            at = normalized.find(old)
                            return _diff(normalized, normalized[:at] + new + normalized[at + len(old):], name, first_line)
                    current, current_size = [], 0
                    line += 1
            else:
                if active and length + current_size <= MAX_DIFF_WINDOW:
                    pieces.extend(current)
                    normalized = _lf("".join(pieces))
                    at = normalized.find(old)
                    return _diff(normalized, normalized[:at] + new + normalized[at + len(old):], name, first_line)
        finally:
            await fragments.aclose()
    return (f"精确替换预览（受影响的完整行超过{MAX_DIFF_WINDOW}字符，非完整diff）\n"
            f"原片段：\n{old[:8000]}\n新片段：\n{new[:8000]}\n",
            {"added_lines": None, "removed_lines": None, "complete": False, "reason": "diff_window_limit"})


def _result(artifacts, path, diff, stats, *, line=1):
    return {"path": path, "status": "succeeded", "changed_line": line,
        "diff": diff[:MAX_OUTPUT_CHARS], "diff_truncated": len(diff) > MAX_OUTPUT_CHARS,
        "diff_format": "unified" if stats["complete"] else "replacement_preview", "change_stats": stats,
        "before_path": str(artifacts.directory / "before.bin"),
        "after_path": str(artifacts.directory / "after.bin"),
        "diff_path": str(artifacts.directory / "change.diff"), **artifacts.references()}


async def create_text_file(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if arguments.keys() - {"path", "content"}:
        raise ToolError("invalid_arguments", "仅支持path和content。")
    content = _text_argument(arguments, "content")
    path = resolve_new_file_path(arguments.get("path"), context.workspace)
    async with mutation_queue(context.workspace, path):
        path = resolve_new_file_path(arguments.get("path"), context.workspace)
        name, raw = path.relative_to(context.workspace).as_posix(), content.encode("utf-8")
        # 创建没有旧文件；参数预算保证这一处差异比较有界。
        diff, stats = _diff("", content, name)
        artifacts = ToolArtifacts(context, "create_text_file", path=name)
        (artifacts.directory / "before.bin").write_bytes(b"")
        (artifacts.directory / "after.bin").write_bytes(raw)
        (artifacts.directory / "change.diff").write_text(diff, encoding="utf-8", newline="\n")
        status = "not_applied"
        try:
            # 小参数写入不让出控制，取消不会把已经创建的目标伪装成未执行。
            with path.open("xb") as target:
                status = "unknown"
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            status = "succeeded"
        except FileExistsError as exc:
            raise ToolError("file_exists", "创建时目标已存在，未覆盖。", details=artifacts.references()) from exc
        except OSError as exc:
            raise ToolError("file_write_error", "创建未确认完成；可能留有部分内容，请核对收据和实际文件。",
                details={"errno": exc.errno, "winerror": getattr(exc, "winerror", None), **artifacts.references()}) from exc
        finally:
            artifacts.finish_mutation(status)
        return {**_result(artifacts, name, diff, stats), **file_evidence(path), "sha256": hashlib.sha256(raw).hexdigest(), "sha256_scope": "full_file"}


async def edit_text_file(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if arguments.keys() - {"path", "old_text", "new_text", "expected_version"}:
        raise ToolError("invalid_arguments", "仅支持path、old_text、new_text、expected_version。")
    old, new = _lf(_text_argument(arguments, "old_text")), _lf(_text_argument(arguments, "new_text"))
    expected = arguments.get("expected_version")
    if not old or (expected is not None and (not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None)):
        raise ToolError("invalid_arguments", "old_text须非空；expected_version须使用读取结果的file_version。")
    path = resolve_workspace_path(arguments.get("path"), context.workspace, kind="file")
    async with mutation_queue(context.workspace, path):
        path = resolve_workspace_path(arguments.get("path"), context.workspace, kind="file")
        name = path.relative_to(context.workspace).as_posix()
        if expected is not None and file_version(path.stat()) != expected:
            raise ToolError("file_changed", "文件已变化，未修改；请重新读取。", details=file_evidence(path))
        artifacts = ToolArtifacts(context, "edit_text_file", path=name)
        snapshot = artifacts.directory / "before.bin"
        candidate = path.with_name(f".tool-{artifacts.directory.name}.pending")
        status = "not_applied"
        try:
            before, before_hash = await _snapshot(path, snapshot)
            if expected is not None and file_version(before) != expected:
                raise ToolError("file_changed", "保存快照前文件已变化，未写回。")
            begin, end, line, newline = await _locate(snapshot, old)
            if old == new:
                if file_version(path.stat()) != file_version(before):
                    raise ToolError("file_changed", "扫描期间文件变化，未把快照当作当前版本。")
                status = "unchanged"
                return {"path": name, "status": "unchanged", "changed_line": None, "diff": "", "change_stats": {"added_lines": 0, "removed_lines": 0, "complete": True},
                        **file_evidence(path, before), "sha256": before_hash, "sha256_scope": "full_file", **artifacts.references()}
            artifacts.finish_mutation("prepared", candidate_path=str(candidate), before_sha256=before_hash, before_version=file_version(before))
            after_hash = await _compose(snapshot, candidate, begin, end, new.replace("\n", newline).encode("utf-8"))
            await _snapshot(candidate, artifacts.directory / "after.bin")
            diff, stats = await _change_diff(snapshot, begin, end, old, new, name)
            (artifacts.directory / "change.diff").write_text(diff, encoding="utf-8", newline="\n")
            artifacts.finish_mutation("prepared", after_sha256=after_hash, change_stats=stats)
            if (resolve_workspace_path(arguments["path"], context.workspace, kind="file") != path
                    or file_version(path.stat()) != file_version(before) or await hash_file(path) != before_hash):
                raise ToolError("file_changed", "准备期间文件变化，未覆盖；原快照与候选均已保留。")
            # 最后一次yield之后再核对版本，应用内同一路径仍处于串行锁中。
            if file_version(path.stat()) != file_version(before):
                raise ToolError("file_changed", "写回前检测到文件变化，未覆盖。")
            os.chmod(candidate, before.st_mode)
            os.replace(candidate, path)
            status = "succeeded"
            return {**_result(artifacts, name, diff, stats, line=line), **file_evidence(path),
                    "before_sha256": before_hash, "sha256": after_hash, "sha256_scope": "full_file"}
        except UnicodeError as exc:
            raise ToolError("unsupported_encoding", "文件不是有效UTF-8，不猜测编码后覆盖。") from exc
        except OSError as exc:
            raise ToolError("file_write_error", "快照或写回失败；不会清理原快照和候选，请先检查实际文件。",
                details={"errno": exc.errno, "winerror": getattr(exc, "winerror", None), **artifacts.references()}) from exc
        finally:
            # 不删除失败候选，不自动回滚；取消也留下明确的未写回事实。
            artifacts.finish_mutation(status)
