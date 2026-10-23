"""只读元数据和有界二进制窗口，不猜文件格式或文本编码。"""
import os
import re
from typing import Any

from ...llm.messages import ToolSchema
from ..contracts import ToolContext, ToolDefinition, ToolDisplay, ToolError
from .evidence import file_evidence, file_version, hash_file, stat_details, validate_read_snapshot
from .limits import MAX_BINARY_BYTES
from .paths import resolve_workspace_path


def create_stat_tool() -> ToolDefinition:
    return ToolDefinition(ToolSchema(name="stat_file", description=(
        ("获取指定路径的文件或目录元数据，包括是否存在、条目类型、精确字节大小（`bytes`）、UTC 修改时间（`mtime_utc`）及stat版本，默认不读取文件正文或计算全文哈希，也不诊断编码。适合在读取超大文"
            "件前先评估体积，或在编辑/命令中断后核实目标文件是否已被创建或修改。只有显式传sha256=true才对普通文件流式计算全文SHA256，受工具时限约束；目录不支持全文哈希，目录大小不表示递归总量。")),
        parameters={"type": "object", "additionalProperties": False, "properties": {
            "path": {"type": "string"}, "sha256": {"type": "boolean", "default": False}}, "required": ["path"]}), execute=stat_file,
        display=ToolDisplay("metadata", "file", "检查", "检查了{n}个路径"), default_profiles=("desktop_work",))


def create_read_bytes_tool() -> ToolDefinition:
    return ToolDefinition(ToolSchema(name="read_bytes", description=(
        ("从指定文件的字节偏移量（`offset`）处读取有界长度（`length`）的原始字节窗口（返回十六进制/字节表示及元数据）。用于核对二进制文件头（Magic Bytes）、检查 BOM/特殊编码字节序列，或排"
            "查非纯文本资源格式。单次最多4,096字节，offset从0开始，用next_offset续读；可传expected_version检测变化，文件总大小不限。")),
        parameters={"type": "object", "additionalProperties": False, "properties": {
            "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0, "default": 0},
            "length": {"type": "integer", "minimum": 1, "maximum": MAX_BINARY_BYTES, "default": 256},
            "expected_version": {"type": "string"}}, "required": ["path"]}), execute=read_bytes,
        display=ToolDisplay("read", "file", "读取", "读取了{n}个文件"), default_profiles=("desktop_work",))


async def stat_file(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if arguments.keys() - {"path", "sha256"} or type(arguments.get("sha256", False)) is not bool:
        raise ToolError("invalid_arguments", "仅支持path与布尔sha256。")
    path = resolve_workspace_path(arguments.get("path"), context.workspace, kind="file_or_directory")
    before = path.stat()
    data = {"path": path.relative_to(context.workspace).as_posix(), **file_evidence(path, before),
            "type": "directory" if path.is_dir() else "file", "sha256": None, "sha256_scope": None}
    if arguments.get("sha256", False):
        if not path.is_file():
            raise ToolError("not_a_file", "全文SHA256只用于普通文件；目录大小不表示递归内容大小。", details=file_evidence(path, before))
        digest = await hash_file(path)
        try:
            after = path.stat()
        except OSError as exc:
            raise ToolError("file_changed", "哈希读取后无法重新确认目标路径。",
                            details={"resolved_path": str(path), "operation": "stat_then_hash",
                                     "before": stat_details(before), "after": None,
                                     "computed_sha256": digest, "sha256_scope": "completed_hash_read",
                                     "errno": exc.errno, "winerror": getattr(exc, "winerror", None)}) from exc
        if file_version(after) != file_version(before):
            raise ToolError("file_changed", "文件在元数据与哈希读取之间发生变化，请重试。",
                            details={"resolved_path": str(path), "operation": "stat_then_hash",
                                     "before": stat_details(before), "after": stat_details(after),
                                     "computed_sha256": digest, "sha256_scope": "completed_hash_read"})
        data.update(sha256=digest, sha256_scope="full_file")
    return data


async def read_bytes(arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if arguments.keys() - {"path", "offset", "length", "expected_version"}:
        raise ToolError("invalid_arguments", "仅支持path、offset、length、expected_version。")
    offset, length = arguments.get("offset", 0), arguments.get("length", 256)
    expected = arguments.get("expected_version")
    if (type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= MAX_BINARY_BYTES
            or (expected is not None and (not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None))):
        raise ToolError("invalid_arguments", "offset须非负，length须为1到4096，版本须来自先前读取结果。")
    path = resolve_workspace_path(arguments.get("path"), context.workspace, kind="file")
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        path_before = path.stat()
        version = file_version(path_before)
        if expected is not None and expected not in {version, file_version(before)}:
            raise ToolError("file_changed", "文件已变化，未续读旧版本。", details=file_evidence(path, before))
        source.seek(offset)
        raw = source.read(length)
        evidence, metadata_changed = await validate_read_snapshot(source, path, before, path_before)
    finish = offset + len(raw)
    return {"path": path.relative_to(context.workspace).as_posix(), **file_evidence(path, evidence), "metadata_changed": metadata_changed,
            "offset": offset, "requested_length": length, "length": len(raw), "hex": raw.hex(" "),
            "ascii": "".join(chr(byte) if 32 <= byte <= 126 else "." for byte in raw),
            "eof": finish >= before.st_size, "out_of_range": offset > before.st_size,
            "next_offset": finish if finish < before.st_size else None}
