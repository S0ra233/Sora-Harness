"""只重试可恢复的网络失败；协议错误和取消不重发。"""
import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar
from time import monotonic
from uuid import uuid4

import httpx

from ..attempts import ModelAttemptEvent, ModelAttemptSink
from ..contracts import TextSink
from ..diagnostics import RequestTrace
from .stream_events import StreamFormatError, StreamInterruptedError, StreamProgress, stream_progress


class ProviderError(RuntimeError):
    """可以展示的连接、服务或响应错误；不包含远端正文及密钥。"""


def http_error(exc: Exception) -> ProviderError:
    """区分等待超时和真实中断；原异常通过 cause 保留供定位。"""
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError("连接或响应等待超时，请检查网络或调整超时设置。")
    if isinstance(exc, httpx.HTTPStatusError):
        return ProviderError(f"模型服务拒绝请求（HTTP {exc.response.status_code}）；请检查协议、密钥、模型和参数。")
    if isinstance(exc, (ProviderError, StreamFormatError)):
        return ProviderError(str(exc))
    return ProviderError("无法请求模型服务，请检查接口地址、网络和代理设置。")


RETRYABLE_ERRORS = (
    httpx.NetworkError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout,
    httpx.RemoteProtocolError, httpx.ProxyError, StreamInterruptedError,
)
T = TypeVar("T")
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def _retryable(exc: Exception) -> bool:
    return isinstance(exc, RETRYABLE_ERRORS) or (
        isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in RETRYABLE_STATUS)


def _retry_delay(exc: Exception, attempt: int) -> float:
    """有限退避；只读取服务端等待时间，不回显响应正文或地址。"""
    if isinstance(exc, httpx.HTTPStatusError):
        value = exc.response.headers.get("retry-after")
        if value:
            try:
                seconds = float(value)
            except ValueError:
                try:
                    stamp = parsedate_to_datetime(value)
                    seconds = (stamp.replace(tzinfo=stamp.tzinfo or timezone.utc) - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    seconds = -1
            if 0 <= seconds <= 60:
                return seconds
            if seconds > 60:
                return 60.0
    ceiling = min(20.0, 2.0 ** min(attempt - 1, 5))
    return random.uniform(ceiling / 2, ceiling)


async def request_with_retries(
    operation: Callable[[TextSink], Awaitable[T]], *, max_attempts: int,
    on_attempt: ModelAttemptSink | None = None, on_text: TextSink | None = None,
    trace: RequestTrace | None = None,
) -> T:
    """operation 捕获同一份 payload；连接复用和关闭由 HTTP 层负责。"""
    request_id = trace.request_id if trace is not None else uuid4().hex
    for number in range(1, max_attempts + 1):
        # 即使上一尝试立即失败，也先允许事件循环处理用户取消。
        await asyncio.sleep(0)
        fragments: list[str] = []
        progress = StreamProgress()

        def notify(phase: str, *, error: str | None = None, retrying: bool = False,
                   duration_seconds: float | None = None) -> ModelAttemptEvent:
            event = ModelAttemptEvent(request_id, number, phase,
                partial_text="".join(fragments) if phase in ("failed", "cancelled") else "",
                error=error, retrying=retrying, duration_seconds=duration_seconds)
            if on_attempt is not None:
                on_attempt(event)
            return event

        def accept_text(text: str) -> None:
            if text:
                progress.has_output = True
            if on_attempt is not None:
                fragments.append(text)
            if on_text is not None:
                on_text(text)

        event = notify("started")
        if trace is not None:
            await trace.attempt(event)
        started = monotonic()
        progress_token = stream_progress.set(progress)
        try:
            value = await operation(accept_text)
        except asyncio.CancelledError:
            event = notify("cancelled")
            if trace is not None:
                await trace.attempt(event)
            raise
        except (httpx.RequestError, httpx.InvalidURL, httpx.HTTPStatusError, StreamFormatError, ProviderError) as exc:
            retrying = _retryable(exc) and not progress.has_output and number < max_attempts
            error = http_error(exc)
            if progress.has_output and _retryable(exc):
                error = ProviderError(str(error) + "本次已收到模型输出，未自动重发；已显示片段不是完整回答，请手动决定是否继续。")
            event = notify("failed", error=str(error), retrying=retrying)
            if trace is not None:
                await trace.attempt(event)
            if not retrying:
                detail = f"已尝试 {number} 次，请求停止。" if _retryable(exc) else ""
                raise ProviderError(str(error) + detail) from exc
            # 等待可被取消；不把退避时间计入下一次成功请求的TPS。
            await asyncio.sleep(_retry_delay(exc, number))
        else:
            # 每次重发单独计时；工具执行发生在返回以后，不进入本次请求耗时。
            event = notify("received", duration_seconds=monotonic() - started)
            if trace is not None:
                await trace.attempt(event)
            return value
        finally:
            stream_progress.reset(progress_token)
