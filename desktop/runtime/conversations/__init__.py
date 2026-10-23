"""连续会话的配置、事实和本地存取入口。"""
from .models import CompactionRecord, Conversation, ConversationInput, ConversationSettings, ConversationSummary, ConversationTurn, TurnOptions
from .store import ConversationStore
from .service import CompactionSaveError, ConversationSaveError, ConversationService

__all__ = [
    "Conversation", "ConversationInput", "ConversationSettings", "ConversationSummary", "ConversationTurn", "ConversationStore",
    "ConversationService", "ConversationSaveError",
    "CompactionRecord", "CompactionSaveError", "TurnOptions",
]
