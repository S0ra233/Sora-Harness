"""通用运行状态与结果通知，不依赖桌面界面。"""
from dataclasses import dataclass
from enum import Enum
from traceback import walk_tb
from typing import Any

from ..llm.models import ModelRequest
from ..llm.attempts import ModelAttemptEvent


class RunState(str, Enum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class RunEvent:
    """关联一次运行；结果类型仍由对应插件决定。"""

    run_id: str
    plugin_id: str
    action: str
    state: RunState
    result: Any = None
    error: str | None = None
    text_delta: str | None = None
    request: ModelRequest | None = None
    diagnostics: str | None = None
    attempt: ModelAttemptEvent | None = None


def exception_diagnostics(exc: Exception) -> str:
    """只提取调用位置，不输出异常正文、源码行或局部变量。"""
    locations = [f"异常类型：{type(exc).__name__}"]
    for frame, line_number in walk_tb(exc.__traceback__):
        code = frame.f_code
        locations.append(f"{code.co_filename}:{line_number} · {code.co_name}")
    return "\n".join(locations)
