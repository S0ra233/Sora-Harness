"""每轮独立连接池；每个连接由同一任务持有，工具调用不跨Qt线程复用。"""
import asyncio
import sys
from contextlib import asynccontextmanager

from .client import list_tools, open_client
from ..tools.contracts import ToolError


class _Connection:
    def __init__(self, server):
        self.server = server
        self.queue = asyncio.Queue()
        self.ready = asyncio.get_running_loop().create_future()
        self.active = None
        self.pending = None
        self.closed = False
        self.worker = asyncio.create_task(self._run(), name="mcp:" + server.server_id)

    async def _run(self):
        try:
            # asyncio时限不压入AnyIO取消栈；SDK连接由本任务持续持有至退出。
            async with asyncio.timeout(self.server.connect_timeout):
                manager = open_client(self.server)
                client = await manager.__aenter__()
                try:
                    live = {tool["name"]: tool for tool in await list_tools(client)}
                except BaseException:
                    await manager.__aexit__(*sys.exc_info())
                    raise
            try:
                self.ready.set_result(live)
                while True:
                    request = await self.queue.get()
                    if request is None:
                        break
                    name, arguments, future = request
                    if future.cancelled():
                        continue
                    self.pending = future
                    self.active = asyncio.create_task(client.session.call_tool(name, arguments,
                        read_timeout_seconds=self.server.call_timeout))
                    def cancel_call(done, call=self.active):
                        if done.cancelled() and not call.done():
                            call.cancel()
                    future.add_done_callback(cancel_call)
                    try:
                        result = await self.active
                        if not future.done():
                            future.set_result(result)
                    except asyncio.CancelledError:
                        if asyncio.current_task().cancelling():
                            if not future.done():
                                future.set_exception(ToolError("mcp_connection_closed", "MCP连接中断；执行状态未知，未自动重试。"))
                            raise
                        if not future.done():
                            future.set_exception(ToolError("mcp_connection_closed", "MCP调用被服务连接中断，未自动重试。"))
                            self.closed = True
                            break
                    except Exception as exc:
                        if not future.done():
                            future.set_exception(ToolError("mcp_call_failed", "MCP调用失败；未自动重试，请先核对服务端是否已经执行。",
                                details={"server_id": self.server.server_id, "remote_tool": name, "error_type": type(exc).__name__, "outcome": "unknown"}))
                        # 未确认的失败不复用连接，不在同一服务上继续发副作用请求。
                        self.closed = True
                        break
                    finally:
                        self.active = None
                        self.pending = None
            finally:
                await manager.__aexit__(*sys.exc_info())
        except asyncio.CancelledError:
            if not self.ready.done():
                # SDK内部中断不能被调用者误认为用户按了停止。
                self.ready.set_exception(ToolError("mcp_connection_closed", "MCP建立连接时中断，其他工具仍可继续。"))
            if self.pending is not None and not self.pending.done():
                self.pending.set_exception(ToolError("mcp_connection_closed", "MCP连接中断；服务端执行状态未知，未自动重试。"))
            raise
        except Exception as exc:
            error = ToolError("mcp_connection_failed", "MCP连接不可用，请检查服务配置；其他工具仍可继续。",
                              details={"server_id": self.server.server_id, "error_type": type(exc).__name__})
            if not self.ready.done():
                self.ready.set_exception(error)
            if self.pending is not None and not self.pending.done():
                self.pending.set_exception(error)
        finally:
            self.closed = True
            while not self.queue.empty():
                pending = self.queue.get_nowait()
                if pending is not None and not pending[2].done():
                    pending[2].set_exception(ToolError("mcp_connection_closed", "本轮MCP连接已关闭，未重新执行。"))

    async def call(self, tool, arguments):
        live = await asyncio.shield(self.ready)
        actual = live.get(tool["name"])
        if actual != tool:
            raise ToolError("mcp_schema_changed", "MCP工具声明与本轮快照不同，未执行。请在设置中重新同步工具。",
                            details={"server_id": self.server.server_id, "remote_tool": tool["name"]})
        if self.closed or self.worker.done():
            raise ToolError("mcp_connection_closed", "本轮MCP连接已关闭，未重新执行。")
        future = asyncio.get_running_loop().create_future()
        self.queue.put_nowait((tool["name"], arguments, future))
        return await future

    async def close(self):
        if self.active is not None:
            self.active.cancel()
        self.worker.cancel()
        await asyncio.gather(self.worker, return_exceptions=True)
        # 取走未被工具等待的建立连接异常，避免退出时出现未处理Future警告。
        if self.ready.done() and not self.ready.cancelled():
            self.ready.exception()


class McpPool:
    def __init__(self, servers):
        self.servers = {server.server_id: server for server in servers if server.enabled}
        self.connections = {}

    async def call(self, server_id, tool, arguments):
        if server_id not in self.servers:
            raise ToolError("mcp_not_enabled", "本轮未启用此MCP服务。")
        connection = self.connections.get(server_id)
        if connection is None:
            connection = self.connections[server_id] = _Connection(self.servers[server_id])
        try:
            return await connection.call(tool, arguments)
        except asyncio.CancelledError:
            # 仍处于工具修改锁内完成连接关闭，不能让后台调用继续并立即释放锁。
            await connection.close()
            raise

    async def close(self):
        await asyncio.gather(*(item.close() for item in self.connections.values()))


@asynccontextmanager
async def mcp_pool(servers):
    pool = McpPool(servers)
    try:
        yield pool
    finally:
        await pool.close()
