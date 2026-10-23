"""应用持有多个会话控制器；页面绑定与后台任务生命周期分别管理。"""
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ...application import ConversationApplication
from ...application.tasks import TaskRegistry
from ...runtime.conversations import Conversation, ConversationSettings
from ..contracts import CloseBlock
from .controller import ConversationController, ConversationState
from ...tools.catalog import default_tool_names
from .worker import ConversationEvent


class ConversationHub(QObject):
    """按归属路由通知；不保存当前页面，也不限制全应用只能运行一个任务。"""

    state_changed = Signal(object)
    event_received = Signal(object)
    conversation_changed = Signal(object)
    environment_changed = Signal(object)
    branch_created = Signal(object)
    subagent_changed = Signal(object)

    def __init__(self, local_directory: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.local_directory = local_directory.expanduser().resolve()
        self.tasks = TaskRegistry()
        self.application = ConversationApplication(self.local_directory, tasks=self.tasks, human_interaction=True)
        self.application.subagents.changed = self.subagent_changed.emit
        self.subagent_changed.connect(self._subagent_update)
        self._controllers: dict[str, ConversationController] = {}
        # 这是 Qt 通知的收件人索引，执行状态与取消入口统一在 tasks 中。
        self._owners: dict[str, ConversationController] = {}
        self._bindings: dict[ConversationController, int] = {}
        self._cleanup_timer = QTimer(self)
        self._cleanup_timer.setSingleShot(True)
        self._cleanup_timer.setInterval(0)
        self._cleanup_timer.timeout.connect(self._prune_idle)

    def new_conversation(self, settings: ConversationSettings, *,
                         required_programs: Sequence[str] = ()) -> ConversationController:
        """先返回页面可绑定的控制器，Core 创建完成后才登记真实会话编号。"""
        controller = self._make_controller()
        try:
            controller.new_conversation(settings, required_programs=required_programs)
        except Exception:
            self.release_view(controller)
            raise
        return controller

    def open_conversation(self, conversation_id: str, *,
                          tool_names: Sequence[str] | None = None) -> ConversationController:
        """已有运行对象直接绑定内存快照；未加载的会话才后台读取日志。"""
        conversation_id = UUID(conversation_id).hex
        controller = self._controllers.get(conversation_id)
        if controller is not None:
            if tool_names is not None:
                controller.set_tool_names(tool_names)
            self._bindings[controller] += 1
            return controller
        controller = self._make_controller(conversation_id)
        self._controllers[conversation_id] = controller
        try:
            controller.open_conversation(conversation_id, tool_names=tool_names if tool_names is not None else self.application.default_tool_names())
        except Exception:
            self.release_view(controller)
            raise
        return controller

    def get(self, conversation_id: str) -> ConversationController:
        """按 ID 定位已经加载的会话，不改变页面绑定，也不自动重新执行。"""
        conversation_id = UUID(conversation_id).hex
        controller = self._controllers.get(conversation_id)
        if controller is None:
            raise ValueError(f"会话 {conversation_id} 尚未加载，请先打开。")
        return controller

    def release_view(self, controller: ConversationController) -> bool:
        """页面解绑后刷新草稿；运行／未确认数据继续由应用保管。"""
        count = self._bindings.get(controller, 0)
        if count < 1:
            raise ValueError("该控制器没有可释放的页面绑定。")
        saved = controller.flush_drafts()
        options_saved = controller.flush_turn_options()
        self._bindings[controller] = count - 1
        self._cleanup_timer.start()
        return saved and options_saved

    def cancel(self, operation_id: str) -> bool:
        """取消明确任务；页面切换不会改变停止按钮的目标。"""
        controller = self._owners.get(operation_id)
        return controller.cancel(operation_id) if controller is not None else self.tasks.cancel(operation_id)

    def close_block(self) -> CloseBlock | None:
        active = self.tasks.active()
        if active:
            ids = "、".join(task.operation_id for task in active)
            return CloseBlock("后台仍在运行", f"任务 {ids} 仍未退出，包含未显示的会话；请先取消并等待退出后再关闭。")
        # 使用快照遍历；草稿通知只安排稍后的清理，不在检查中移除控制器。
        for controller in tuple(self._bindings):
            block = controller.close_block()
            if block is not None:
                owner = controller.state.conversation_id or controller.state.operation_id or "未创建会话"
                return CloseBlock(block.title, f"会话／任务 {owner}：{block.message}")
        return None

    def _make_controller(self, conversation_id: str | None = None) -> ConversationController:
        controller = ConversationController(
            self.local_directory, self, application=self.application, tasks=self.tasks,
            conversation_id=conversation_id,
        )
        self._bindings[controller] = 1
        controller.state_changed.connect(self._on_state)
        controller.event_received.connect(self._on_event)
        controller.conversation_changed.connect(self._on_conversation)
        controller.environment_changed.connect(self.environment_changed.emit)
        controller.task_finished.connect(self._on_finished)
        controller.branch_created.connect(self.branch_created.emit)
        return controller

    @Slot(object)
    def _on_state(self, state: ConversationState) -> None:
        controller = self.sender()
        if not isinstance(controller, ConversationController) or controller not in self._bindings:
            return
        for operation_id in controller.task_ids:
            self._owners[operation_id] = controller
        self.state_changed.emit(state)
        if self._bindings[controller] == 0 and not controller.busy:
            # 延迟到本次入口返回后再判断，避免发送前保存草稿时释放控制器。
            self._cleanup_timer.start()

    @Slot(object)
    def _on_event(self, event: ConversationEvent) -> None:
        if self._owners.get(event.operation_id) is self.sender():
            self.event_received.emit(event)

    @Slot(object)
    def _on_conversation(self, conversation: Conversation) -> None:
        controller = self.sender()
        if isinstance(controller, ConversationController) and controller in self._bindings:
            self._controllers[conversation.conversation_id] = controller
            self.conversation_changed.emit(conversation)

    @Slot(str)
    def _on_finished(self, operation_id: str) -> None:
        if self._owners.get(operation_id) is self.sender():
            self._owners.pop(operation_id)
            self._cleanup_timer.start()

    @Slot()
    def _prune_idle(self) -> None:
        """空闲且无页面／待存资料时释放显示对象；文件与历史始终保留。"""
        for controller, count in tuple(self._bindings.items()):
            if count or controller.busy or controller.has_pending_changes:
                continue
            if conversation_id := controller.state.conversation_id:
                if self.application.subagents.notices(conversation_id) or any(item["status"] in {"starting", "running", "cancelling"}
                        for item in self.application.subagents.list(conversation_id)):
                    continue
            conversation_id = controller.state.conversation_id
            if self._controllers.get(conversation_id) is controller:
                self._controllers.pop(conversation_id)
            self._bindings.pop(controller)
            # QObject销毁断开显示订阅，持久文件不删除。
            controller.deleteLater()

    @Slot(object)
    def _subagent_update(self, record):
        controller = self._controllers.get(record["parent_id"])
        if controller is not None and record["status"] not in {"starting", "running", "cancelling"}:
            QTimer.singleShot(80, controller.try_subagent_wake)
