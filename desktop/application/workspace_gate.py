"""本应用任务的目录准入策略；不代替文件权限、沙箱或跨进程协调。"""
import os
from pathlib import Path


class WorkspaceBusyError(ValueError):
    """冲突任务未进入执行；调用方保留输入，用户稍后重新提交。"""

    def __init__(self, workspace: Path, operation_id: str) -> None:
        super().__init__(f"工作目录 {workspace} 正在被任务 {operation_id} 使用；请等待它退出后再发送，输入已保留。")
        self.workspace = workspace
        self.operation_id = operation_id


class WorkspaceGate:
    """首版协调相同／父子目录；由 TaskRegistry 在登记锁内调用。"""

    def __init__(self) -> None:
        self._claims: dict[str, Path] = {}
        self._scopes: dict[str, str] = {}

    def claim(self, operation_id: str, workspace: Path | None, *, scope: str | None = None) -> Path | None:
        if workspace is None:
            return None
        # 新项目先占用目标路径，Core 准备阶段才创建目录；已有父路径仍解析符号链接。
        root = workspace.expanduser().resolve()
        if root.exists() and not root.is_dir():
            raise ValueError(f"工作目录不是文件夹：{root}。")
        canonical = Path(os.path.normcase(str(root)))
        for owner, occupied in self._claims.items():
            if canonical.is_relative_to(occupied) or occupied.is_relative_to(canonical):
                if scope is not None and self._scopes.get(owner) == scope and canonical == occupied:
                    continue
                raise WorkspaceBusyError(root, owner)
        self._claims[operation_id] = canonical
        if scope is not None:
            self._scopes[operation_id] = scope
        return root

    def release(self, operation_id: str) -> None:
        self._claims.pop(operation_id, None)
        self._scopes.pop(operation_id, None)
