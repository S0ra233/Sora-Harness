"""应用公共入口按使用加载，单独读取模型配置不提前加载会话执行模块。"""
__all__ = ["ConversationApplication", "ConversationUpdate", "TaskInfo", "TaskRegistry", "WorkspaceBusyError"]


def __getattr__(name: str):
    """保留既有包级导入契约；各入口直接导入模块时不产生整包依赖。"""
    if name in ("ConversationApplication", "ConversationUpdate"):
        from . import conversations
        return getattr(conversations, name)
    if name in ("TaskInfo", "TaskRegistry"):
        from . import tasks
        return getattr(tasks, name)
    if name == "WorkspaceBusyError":
        from .workspace_gate import WorkspaceBusyError
        return WorkspaceBusyError
    raise AttributeError(f"{__name__}没有属性{name}。")
