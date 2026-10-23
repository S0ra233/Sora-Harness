"""模型调用与通知的函数约定，供后端和平台运行模块共同使用。"""
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol

from .models import ModelRequest, ModelResponse
from .attempts import ModelAttemptSink
from .reasoning_output import ReasoningDelta
from .diagnostics import RequestTrace


# 接收统一请求，等待完整响应；具体实现由协议工厂选择。
ModelCaller = Callable[[ModelRequest], Awaitable[ModelResponse]]
# 流式文本通知只用于展示，结束后仍由调用函数返回响应。
TextSink = Callable[[str], None]
ReasoningSink = Callable[[ReasoningDelta], None]
ModelStreamCaller = Callable[[ModelRequest, TextSink], Awaitable[ModelResponse]]
# 观察统一请求快照，不在通知中改变实际发送内容。
RequestSink = Callable[[ModelRequest], None]


class ModelBackend(Protocol):
    """循环需要的已有调用能力；不要求依赖某一家具体 Provider。"""

    async def complete(self, request: ModelRequest, *, on_attempt: ModelAttemptSink | None = None,
                       on_reasoning: ReasoningSink | None = None, trace: RequestTrace | None = None) -> ModelResponse: ...

    async def stream_complete(
        self, request: ModelRequest, on_text: TextSink, *, on_attempt: ModelAttemptSink | None = None,
        on_reasoning: ReasoningSink | None = None,
        trace: RequestTrace | None = None,
    ) -> ModelResponse: ...


# 每次进入都创建本次运行自己的连接范围，不跨事件循环保存客户端。
ModelSessionFactory = Callable[[], AbstractAsyncContextManager[ModelBackend]]
