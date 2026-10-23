"""通过用户已启用的本地MCP转换文档；不安装服务、不启动主模型请求。"""
import asyncio
import hashlib
from pathlib import Path

from ..mcp.pool import mcp_pool
from ..mcp.settings import McpSettings


def document_readers(data_root):
    result = []
    for server in McpSettings(Path(data_root) / "mcp.json").load():
        if not server.enabled or server.transport != "stdio":
            continue
        for tool in server.tools:
            schema = tool["input_schema"]
            if tool["name"] == "convert_to_markdown" and "uri" in schema.get("properties", {}) and not set(schema.get("required", [])) - {"uri"}:
                result.append((server, tool))
    return result


async def convert_document(data_root, path, server_id, tool_name):
    """只接受已同步的转换工具，返回原文件哈希和转换正文，不把失败当空文档。"""
    path = Path(path).resolve(strict=True)
    if path.suffix.lower() not in {".pdf", ".docx"} or not path.is_file():
        raise ValueError("文档转换首批支持PDF与DOCX。")
    def source_hash():
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError("单份文档超过64MiB，请先拆分。")
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    source = await asyncio.to_thread(source_hash)
    match = next(((server, tool) for server, tool in document_readers(data_root)
                  if server.server_id == server_id and tool["name"] == tool_name), None)
    if match is None:
        raise ValueError("文档转换MCP未启用或声明已改变，请在设置中连接并同步工具。")
    server, tool = match
    async with mcp_pool((server,)) as pool:
        async with asyncio.timeout(server.connect_timeout + server.call_timeout):
            raw = await pool.call(server.server_id, tool, {"uri": path.as_uri()})
    if raw.is_error:
        raise ValueError("文档转换服务返回失败；未导入，不自动重试。")
    text = "\n".join(block.text for block in raw.content if block.type == "text")
    if not text.strip() or len(text) > 16 * 1024 * 1024 or "\x00" in text:
        raise ValueError("文档转换结果为空、过大或含空字符；未导入。")
    return {"text": text, "source_sha256": source, "converter": f"mcp:{server_id}:{tool_name}",
            "location_basis": "converted_markdown_lines; not_pdf_page_numbers"}
