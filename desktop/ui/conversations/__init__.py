"""通用会话的 Qt 控制入口；页面布局由调用方提供。"""
from .controller import ConversationController, ConversationState, ConversationView
from .hub import ConversationHub

__all__ = ["ConversationController", "ConversationState", "ConversationView", "ConversationHub"]
