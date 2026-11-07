"""Shared evidence contracts; no agent runtime or production configuration."""

from common.history import (
    HistoryChat,
    HistoryCursor,
    HistoryMessage,
    HistoryPage,
    HistoryQuery,
    HistoryReader,
    MessageRef,
    MessageTime,
    SenderEvidence,
    SourceBinding,
)
from .wechat_sqlite import WeChatSQLiteHistory
from .connection import open_wechat_history
from .digital_human import recent_draft_input
from .knowledge import (
    KnowledgeRef, KnowledgeEvidence, KnowledgeEntity, KnowledgeFact,
    KnowledgeDocument, KnowledgeReader, combine_knowledge,
)
from .dh_knowledge import open_dh_knowledge
from .rpa_knowledge import open_rpa_knowledge
from .assistant_knowledge import open_assistant_knowledge
from common.records import RawRecord, RecordBatch

__all__ = [
    "HistoryChat", "HistoryCursor", "HistoryMessage", "HistoryPage", "HistoryQuery", "HistoryReader",
    "MessageRef", "MessageTime", "SenderEvidence", "SourceBinding", "WeChatSQLiteHistory",
    "open_wechat_history", "recent_draft_input",
    "KnowledgeRef", "KnowledgeEvidence", "KnowledgeEntity", "KnowledgeFact",
    "KnowledgeDocument", "KnowledgeReader", "combine_knowledge",
    "open_dh_knowledge", "open_rpa_knowledge", "open_assistant_knowledge", "RawRecord", "RecordBatch",
]
