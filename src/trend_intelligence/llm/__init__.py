"""Stage 8: offline-by-default Claude client (routing, caps, validation, caching, batches)."""

from trend_intelligence.llm.client import (
    BatchItem,
    BatchItemResult,
    ClaudeClient,
    DailyTaskLimitError,
    LLMError,
    LLMResult,
)
from trend_intelligence.llm.transport import (
    LiveCallsDisabledError,
    OfflineTransport,
    Transport,
    live_allowed,
    make_transport,
)

__all__ = [
    "BatchItem", "BatchItemResult", "ClaudeClient", "DailyTaskLimitError", "LLMError",
    "LLMResult", "LiveCallsDisabledError", "OfflineTransport", "Transport", "live_allowed",
    "make_transport",
]
