"""按已执行调用的记录 ID 查看有界窗口，不开放工作区外任意文件。"""
import codecs
import json
import os
import re
from pathlib import Path

from ..llm.messages import ToolSchema
from .contracts import ToolContext, ToolDefinition, ToolDisplay, ToolError


_FILES = {"receipt": "receipt.json", "result": "result.json", "output": "output.log", "diff": "change.diff", "mcp": "mcp-result.json"}
_MAX_BYTES = 16384


def issued_record_ids(results, directory: Path) -> set[str]:
    """只取本地真实结果的引用；老记录按已配置根恢复UUID，不解析输出正文。"""
    identifiers = set()
    root = directory.resolve()
    for result in results:
        try:
            body = json.loads(result.content)
            if not isinstance(body, dict):
                continue
            identifier = body.get("record_id")
            if isinstance(identifier, str) and re.fullmatch(r"[0-9a-f]{32}", identifier):
                identifiers.add(identifier)
                continue
            # 旧工具结果只有路径引用；这里仅恢复受配置根约束的记录编号。
            path = body.get("artifacts_path")
            if isinstance(path, str):
                resolved = Path(path).resolve()
                if resolved.parent == root and re.fullmatch(r"[0-9a-f]{32}", resolved.name):
                    identifiers.add(resolved.name)
        except (ValueError, OSError, RecursionError):
            continue
    return identifiers


def _record_path(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ToolError("record_not_found", "该工具记录或所选文件不存在；请核对record_id与file。") from exc


def create_record_tool() -> ToolDefinition:
    return ToolDefinition(ToolSchema(name="read_tool_record", description=(
        ("根据工具执行结果中返回的真实 `record_id`，分段读取完整工具收据、长命令原始输出（`output.log`）、完整文件修改差异（Diff）或被截断的 MCP 工具返回正文。当 `run_command"
            "`、`edit_text_file` 或 MCP 工具提示输出已折叠/截断且 `record_access=\"available\"` 时，优先调用本工具并传入 `record_id` 、file 及 offse"
            "t查阅详情；不要尝试用 `read_text_file` 直接打开工作区外的收据存储路径。file为receipt/result/output/diff/mcp，默认receipt；每次最多16,384字节，使"
            "用next_offset续读。只读本会话已签发记录，不接受任意路径；旧结果可取artifacts_path最后一级UUID。")),
        parameters={"type": "object", "additionalProperties": False, "properties": {
            "record_id": {"type": "string"}, "file": {"type": "string", "enum": list(_FILES), "default": "receipt"},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "length": {"type": "integer", "minimum": 4, "maximum": _MAX_BYTES, "default": _MAX_BYTES}},
            "required": ["record_id"]}), execute=read_tool_record,
        display=ToolDisplay("read", "file", "读取", "读取了{n}个文件", "read", "工具记录",
                            ("record_id", "file"), " · "), default_profiles=("desktop_work",))


async def read_tool_record(arguments: dict, context: ToolContext) -> dict:
    identifier, kind = arguments.get("record_id"), arguments.get("file", "receipt")
    offset, length = arguments.get("offset", 0), arguments.get("length", _MAX_BYTES)
    if (arguments.keys() - {"record_id", "file", "offset", "length"}
            or not isinstance(identifier, str) or re.fullmatch(r"[0-9a-f]{32}", identifier) is None
            or not isinstance(kind, str) or kind not in _FILES or type(offset) is not int or offset < 0
            or type(length) is not int or not 4 <= length <= _MAX_BYTES):
        raise ToolError("invalid_arguments", "记录ID须为32位小写十六进制；file须来自声明，offset非负，length为4～16384。")
    if context.artifacts_dir is None:
        raise ToolError("record_unavailable", "本轮未配置工具记录目录。")
    if identifier not in context.artifact_ids:
        raise ToolError("record_not_allowed", "这份记录ID未在本会话的本地工具结果中签发。")
    root = context.artifacts_dir.resolve()
    directory = _record_path(root / identifier)
    if directory.parent != root:
        raise ToolError("record_not_allowed", "记录目录不能通过链接指向其他位置。")
    receipt = directory / "receipt.json"
    if _record_path(receipt).parent != directory:
        raise ToolError("record_not_allowed", "收据不能通过链接指向其他位置。")
    # 收据由本地执行器建立；限制读取大小，不能用模型填写的路径作为权限依据。
    with receipt.open("rb") as source:
        raw_receipt = source.read(65537)
    if len(raw_receipt) > 65536:
        raise ToolError("record_invalid", "收据超过元数据上限，未按记录扩大读取范围。")
    try:
        document = json.loads(raw_receipt)
        owned = (isinstance(document, dict) and document.get("call_id") in context.artifact_calls
                 and os.path.normcase(str(context.workspace.resolve())) == os.path.normcase(str(
                     Path(document.get("workspace", "")).resolve())))
    except (ValueError, TypeError, OSError) as exc:
        raise ToolError("record_invalid", "收据格式或归属无效。") from exc
    if not owned:
        raise ToolError("record_not_allowed", "这份收据不属于本会话已执行的调用和工作目录。")
    available = [name for name, filename in _FILES.items() if (directory / filename).is_file()]
    if kind not in available:
        raise ToolError("record_file_not_found", "该调用没有生成所选记录文件。", details={"available_files": available})
    path = _record_path(directory / _FILES[kind])
    if path.parent != directory or not path.is_file():
        raise ToolError("record_not_allowed", "记录文件不是该收据目录内的普通文件。")
    with path.open("rb") as source:
        size = os.fstat(source.fileno()).st_size
        source.seek(offset)
        raw = source.read(length)
    # UTF-8末尾不完整字符留给下一页；非法命令字节按原命令工具的口径替换。
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    content = decoder.decode(raw, final=offset + len(raw) >= size)
    pending, _ = decoder.getstate()
    consumed = len(raw) - len(pending)
    finish = offset + consumed
    return {"record_id": identifier, "file": kind, "call_id": document["call_id"], "content": content,
            "offset": offset, "requested_length": length, "bytes": consumed, "total_bytes": size, "eof": finish >= size,
            "out_of_range": offset > size, "next_offset": finish if finish < size else None,
            "encoding": "utf-8; invalid bytes replaced", "snapshot_size": True}
