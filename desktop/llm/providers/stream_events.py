"""共用 SSE 分帧，只处理传输格式，不推断某个模型协议的结束状态。"""
import json
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx


class StreamFormatError(ValueError):
    """流的结构或结束状态不符合当前协议。"""


class StreamInterruptedError(StreamFormatError):
    """连接已结束但尚无协议终止事件；与错误 JSON、拒绝等情况分开。"""


@dataclass
class StreamProgress:
    """一次请求的实际产出证据；不把心跳、角色或本地思考提示当成输出。"""
    has_output: bool = False


stream_progress: ContextVar[StreamProgress | None] = ContextVar("model_stream_progress", default=None)


def mark_stream_output() -> None:
    """协议读取器确认产出后标记当前尝试，独立运行之间不共享状态。"""
    progress = stream_progress.get()
    if progress is not None:
        progress.has_output = True


async def iter_event_data(response: httpx.Response) -> AsyncIterator[str]:
    """按空行分帧并合并多行 data；忽略事件名、注释和心跳空帧。"""
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if data_lines:
                yield "\n".join(data_lines)
                data_lines.clear()
        elif line.startswith("data:"):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    if data_lines:
        yield "\n".join(data_lines)


def parse_event_data(event_data: str) -> dict[str, Any]:
    """读取 JSON 事件；服务错误不回显可能包含敏感内容的正文。"""
    try:
        data = json.loads(event_data)
    except json.JSONDecodeError as exc:
        raise StreamFormatError("流式事件不是有效 JSON。") from exc
    if not isinstance(data, dict) or data.get("error") or data.get("type") == "error":
        raise StreamFormatError("模型服务返回了无效或失败的流式事件。")
    return data
