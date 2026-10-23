"""统一登记多个运行任务；执行器和界面选择不属于注册表。"""
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock

from .workspace_gate import WorkspaceGate


@dataclass(frozen=True)
class TaskInfo:
    operation_id: str
    kind: str
    conversation_id: str | None
    workspace: Path | None = None
    phase: str = "running"


@dataclass
class _TaskEntry:
    info: TaskInfo
    cancel: Callable[[], bool]


class TaskRegistry:
    """按 ID 管理在途任务；同会话历史的串行规则由会话入口负责。"""

    def __init__(self, workspace_gate: WorkspaceGate | None = None) -> None:
        self._tasks: dict[str, _TaskEntry] = {}
        self._lock = Lock()
        self._workspaces = workspace_gate if workspace_gate is not None else WorkspaceGate()

    def register(self, operation_id: str, kind: str, conversation_id: str | None,
                 cancel: Callable[[], bool], *, workspace: Path | None = None,
                 parent_operation: str | None = None) -> TaskInfo:
        """原子完成准入与登记；不等待模型或工具执行，不持有 Qt 对象类型。"""
        with self._lock:
            if operation_id in self._tasks:
                raise ValueError(f"任务编号已登记：{operation_id}。")
            if parent_operation is not None:
                parent = self._tasks.get(parent_operation)
                if parent is None or parent.info.conversation_id != conversation_id:
                    raise ValueError("子任务没有可继承的活动父目录租约。")
                workspace = parent.info.workspace
            root = self._workspaces.claim(operation_id, workspace, scope=conversation_id)
            info = TaskInfo(operation_id, kind, conversation_id, root)
            self._tasks[operation_id] = _TaskEntry(info, cancel)
            return info

    def get(self, operation_id: str) -> TaskInfo | None:
        with self._lock:
            entry = self._tasks.get(operation_id)
            return entry.info if entry is not None else None

    def active(self) -> tuple[TaskInfo, ...]:
        """终态已通知但执行器未退出的任务也保留在这里。"""
        with self._lock:
            return tuple(entry.info for entry in self._tasks.values())

    def bind_conversation(self, operation_id: str, conversation_id: str) -> None:
        """创建完成才绑定 Core 的真实编号；不提前伪造持久会话 ID。"""
        with self._lock:
            entry = self._tasks[operation_id]
            entry.info = replace(entry.info, conversation_id=conversation_id)

    def update_phase(self, operation_id: str, phase: str) -> None:
        with self._lock:
            entry = self._tasks[operation_id]
            entry.info = replace(entry.info, phase=phase)

    def cancel(self, operation_id: str) -> bool:
        with self._lock:
            entry = self._tasks.get(operation_id)
        # 取消可能触发执行器回调，不能在登记锁内调用外部代码。
        if entry is None or not entry.cancel():
            return False
        with self._lock:
            if self._tasks.get(operation_id) is entry:
                entry.info = replace(entry.info, phase="cancelling")
        return True

    def release(self, operation_id: str) -> None:
        """所有者在执行器真正退出后调用；收到终态通知时仍不释放目录。"""
        with self._lock:
            self._tasks.pop(operation_id, None)
            self._workspaces.release(operation_id)
