"""官方SDK的连接与发现边界；不导入GUI、不改模型工具前缀。"""
from contextlib import asynccontextmanager, AsyncExitStack
import json
import os
import re

from .settings import McpServer
from ..tools.contracts import ToolError


def _values(values):
    def expand(match):
        name = match.group(1)
        if name not in os.environ:
            raise ToolError("mcp_credentials_missing", f"MCP需要环境变量 {name}，未建立连接。")
        return os.environ[name]
    return {key: re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", expand, value) for key, value in values.items()}


@asynccontextmanager
async def open_client(server: McpServer):
    """连接和退出保持在同一异步任务；不开放采样、认证弹窗或自动工具重试。"""
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import streamable_http_client
    async with AsyncExitStack() as stack:
        if server.transport == "stdio":
            # SDK使用最小环境；不继承模型密钥。stderr不回显任意第三方日志。
            errlog = stack.enter_context(open(os.devnull, "w", encoding="utf-8"))
            transport = stdio_client(StdioServerParameters(command=server.command, args=list(server.args),
                cwd=server.cwd or None, env=_values(server.env)), errlog=errlog)
        else:
            import httpx2
            http = await stack.enter_async_context(httpx2.AsyncClient(headers=_values(server.headers),
                timeout=httpx2.Timeout(server.connect_timeout, read=server.call_timeout)))
            transport = streamable_http_client(server.url, http_client=http)
        client = await stack.enter_async_context(Client(transport, read_timeout_seconds=server.call_timeout, cache=None))
        yield client


async def list_tools(client) -> tuple[dict, ...]:
    """按真实游标列举，达到边界时报错，不公布半份工具目录。"""
    cursor, seen, tools = None, set(), {}
    for _ in range(100):
        page = await client.list_tools(cursor=cursor, cache_mode="bypass")
        for tool in page.tools:
            if tool.name in tools:
                raise ToolError("mcp_duplicate_tool", "MCP服务返回重复工具名称。")
            tools[tool.name] = {"name": tool.name, "description": tool.description or "",
                                 "input_schema": tool.input_schema}
        if len(tools) > 2000 or len(json.dumps(tools, ensure_ascii=False)) > 4 * 1024 * 1024:
            raise ToolError("mcp_catalog_limit", "MCP工具目录超过当前登记预算，未替换旧目录。")
        cursor = page.next_cursor
        if not cursor:
            return tuple(tools[name] for name in sorted(tools))
        if cursor in seen:
            raise ToolError("mcp_invalid_cursor", "MCP工具目录游标重复。")
        seen.add(cursor)
    raise ToolError("mcp_catalog_limit", "MCP工具目录分页过多，未替换旧目录。")


async def discover(server: McpServer) -> tuple[dict, ...]:
    """用于用户主动同步；连接时限包含协商和完整目录读取。"""
    import anyio
    with anyio.fail_after(server.connect_timeout):
        async with open_client(server) as client:
            return await list_tools(client)
