"""MCP连接配置和显式同步的工具声明；读取配置不启动服务器。"""
from dataclasses import asdict, dataclass, field
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit
from uuid import UUID

from ..storage.atomic import write_text_atomic


@dataclass(frozen=True)
class McpServer:
    server_id: str
    name: str
    transport: str = "stdio"
    enabled: bool = False
    command: str = ""
    args: tuple[str, ...] = ()
    cwd: str = ""
    url: str = ""
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    connect_timeout: float = 20
    call_timeout: float = 120
    tools: tuple[dict, ...] = ()

    def __post_init__(self):
        if not isinstance(self.server_id, str) or UUID(self.server_id).hex != self.server_id:
            raise ValueError("MCP服务编号不正确。")
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 120:
            raise ValueError("请填写不超过120字的服务名称。")
        if self.transport not in {"stdio", "streamable_http"} or type(self.enabled) is not bool:
            raise ValueError("MCP传输方式或开关不正确。")
        if any(not isinstance(value, str) or "\x00" in value for value in (self.command, self.cwd, self.url)):
            raise ValueError("连接参数须为不含空字符的文字。")
        if self.transport == "stdio" and not self.command.strip():
            raise ValueError("请填写服务启动程序。")
        if self.cwd and not Path(self.cwd).expanduser().is_absolute():
            raise ValueError("启动目录须为绝对路径。")
        if self.transport == "streamable_http":
            parsed = urlsplit(self.url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError("MCP地址须为HTTP(S)地址，认证使用请求头。")
        if not isinstance(self.args, (list, tuple)) or any(not isinstance(arg, str) or "\x00" in arg for arg in self.args):
            raise ValueError("启动参数须为文字列表。")
        for values in (self.env, self.headers):
            if not isinstance(values, dict) or any(not isinstance(key, str) or not key or not isinstance(value, str)
                    or any(char in key + value for char in "\x00\r\n") for key, value in values.items()):
                raise ValueError("环境变量和请求头须为单行文字键值。")
        for timeout in (self.connect_timeout, self.call_timeout):
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 3600:
                raise ValueError("MCP时限须在1至3600秒之间。")
        if not isinstance(self.tools, (list, tuple)) or len(self.tools) > 2000:
            raise ValueError("MCP工具声明数量不正确。")
        names = set()
        for tool in self.tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"] or tool["name"] in names:
                raise ValueError("MCP工具名称为空或重复。")
            if not isinstance(tool.get("description", ""), str) or not isinstance(tool.get("input_schema"), dict):
                raise ValueError("MCP工具说明或参数声明不正确。")
            if tool["input_schema"].get("type", "object") != "object":
                raise ValueError("当前模型工具接口要求object类型参数。")
            names.add(tool["name"])
        object.__setattr__(self, "args", tuple(self.args))
        object.__setattr__(self, "env", dict(self.env))
        object.__setattr__(self, "headers", dict(self.headers))
        object.__setattr__(self, "tools", tuple(deepcopy(self.tools)))

    def connection_key(self):
        """连接改变使旧声明失效；开关、名称和时限不会悄悄重写声明。"""
        return (self.transport, self.command, self.args, self.cwd, self.url, self.env, self.headers)


def tool_name(server_id: str, remote: str) -> str:
    import re
    label = re.sub(r"[^A-Za-z0-9_]", "_", remote)[:14] or "tool"
    return f"mcp_{server_id}_{label}_{hashlib.sha256(remote.encode()).hexdigest()[:10]}"


def delegated_snapshot(servers):
    """子日志只存连接指纹和公开声明，不复制请求头或环境变量里的凭据。"""
    snapshots = []
    for server in servers:
        value = asdict(server)
        fingerprint = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        snapshots.append({"server_id": server.server_id, "fingerprint": fingerprint, "tools": deepcopy(value["tools"])})
    return snapshots


def restore_delegated(settings, snapshots):
    """启动前重新取得本地连接，只有完全匹配父快照才可继承。"""
    current = {server.server_id: server for server in settings.load()}
    restored = []
    for snapshot in snapshots:
        server = current.get(snapshot["server_id"])
        if server is None or delegated_snapshot((server,))[0]["fingerprint"] != snapshot["fingerprint"]:
            raise ValueError("子任务的MCP连接配置已改变，未改用新版连接。请重新发起子任务。")
        restored.append(server)
    return tuple(restored)


class McpSettings:
    def __init__(self, path: Path):
        self.path, self._lock = path, RLock()

    def load(self) -> tuple[McpServer, ...]:
        with self._lock:
            try:
                raw = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return ()
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("MCP设置超过8MiB，原文件保留。")
            try:
                data = json.loads(raw)
                if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1 or not isinstance(data.get("servers"), list):
                    raise ValueError("MCP设置格式不正确。")
                values = tuple(McpServer(**value) for value in data["servers"])
                if len({value.server_id for value in values}) != len(values):
                    raise ValueError("MCP服务编号重复。")
                return values
            except (ValueError, TypeError, RecursionError) as exc:
                raise ValueError("MCP设置读取失败，原文件保留。请在设置页修正配置。") from exc

    def save(self, server: McpServer, *, synced=False):
        with self._lock:
            values = {item.server_id: item for item in self.load()}
            previous = values.get(server.server_id)
            if not synced and previous and previous.connection_key() != server.connection_key():
                from dataclasses import replace
                server = replace(server, tools=())
            values[server.server_id] = server
            write_text_atomic(self.path, json.dumps({"version": 1, "servers": [asdict(item) for item in values.values()]},
                                                    ensure_ascii=False, indent=2, allow_nan=False))
        return server

    def remove(self, identifier: str):
        with self._lock:
            values = [asdict(item) for item in self.load() if item.server_id != identifier]
            write_text_atomic(self.path, json.dumps({"version": 1, "servers": values}, ensure_ascii=False, indent=2))
