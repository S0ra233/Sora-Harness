"""将已同步的MCP声明适配成现有ToolDefinition，结果仍沿统一收据路线。"""
from copy import deepcopy
import asyncio
from functools import partial
import json

from .settings import tool_name
from ..llm.messages import ToolSchema
from ..tools.artifacts import ToolArtifacts
from ..tools.contracts import ToolDefinition, ToolDisplay, ToolError


async def _execute(server_id, tool, arguments, context):
    if context.mcp is None:
        raise ToolError("mcp_not_bound", "本次运行没有MCP连接作用域。")
    artifacts = ToolArtifacts(context, tool_name(server_id, tool["name"]), server_id=server_id, remote_tool=tool["name"])
    try:
        raw = await context.mcp.call(server_id, tool, arguments)
    except (ToolError, asyncio.CancelledError):
        artifacts.finish_mutation("interrupted", outcome="unknown")
        raise
    content = raw.model_dump(mode="json", by_alias=True, exclude_none=True)
    references = artifacts.references()
    text, omitted = [], []
    for block in raw.content:
        if block.type == "text":
            text.append(block.text)
        else:
            omitted.append(block.type)
    structured = raw.structured_content
    rendered = "\n".join(text)
    structured_size = len(json.dumps(structured, ensure_ascii=False)) if structured is not None else 0
    truncated = len(rendered) + structured_size > 24000
    result = {"server_id": server_id, "remote_tool": tool["name"], "text": rendered[:24000],
              "truncated": truncated, **references}
    if structured is not None:
        if not truncated:
            result["structured_content"] = structured
        else:
            result["structured_preview"] = json.dumps(structured, ensure_ascii=False)[:max(0, 24000 - len(result["text"]))]
    if omitted:
        result["omitted_content_types"] = sorted(set(omitted))
        result["warnings"] = ["非文本内容保留在mcp-result.json，未转为模型图片或音频输入。"]
    if truncated or omitted:
        result["full_result"] = {"tool": "read_tool_record", "record_id": artifacts.directory.name, "file": "mcp"}
    try:
        (artifacts.directory / "mcp-result.json").write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
        artifacts.update("failed" if raw.is_error else "succeeded")
    except OSError:
        # 工具已返回；保存故障不能把成功标成调用失败并诱导再次执行。
        result["record_warning"] = "MCP已返回，但完整结果或终态收据未保存；请勿直接重跑。"
        result.pop("full_result", None)
    if raw.is_error:
        raise ToolError("mcp_tool_error", "MCP服务报告工具执行失败。", details=result)
    return result


def register_mcp_tools(registry, servers):
    for server in servers:
        if not server.enabled:
            continue
        definitions = []
        for tool in server.tools:
            parameters = deepcopy(tool["input_schema"])
            parameters.setdefault("type", "object")
            definitions.append(ToolDefinition(ToolSchema(tool_name(server.server_id, tool["name"]),
                tool.get("description", ""), parameters), partial(_execute, server.server_id, tool),
                ToolDisplay(category="mcp", icon="plugin", verb="调用", summary="调用了{n}个MCP工具", heading=server.name,
                            result_subject_fields=("remote_tool",)),
                timeout_seconds=server.connect_timeout + server.call_timeout, default_profiles=("desktop_work", "cli_work")))
        try:
            registry.register_many(definitions, source="mcp:" + server.server_id)
        except ValueError as exc:
            registry.registration_errors.append(f"MCP {server.server_id} 登记失败（{type(exc).__name__}）。")
