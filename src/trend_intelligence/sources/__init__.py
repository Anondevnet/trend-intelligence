"""Source adapter registry."""

from __future__ import annotations

from trend_intelligence.sources.ai_sources import (
    ArxivAdapter,
    HuggingFaceAdapter,
    McpRegistryAdapter,
)
from trend_intelligence.sources.base import SourceAdapter
from trend_intelligence.sources.crypto_sources import (
    DefiLlamaAdapter,
    PolymarketAdapter,
    SolanaRpcAdapter,
)
from trend_intelligence.sources.feeds import RedditAdapter, RssAdapter
from trend_intelligence.sources.github import GitHubAdapter
from trend_intelligence.sources.hackernews import HackerNewsAdapter
from trend_intelligence.sources.mastodon import MastodonAdapter
from trend_intelligence.sources.x import XAdapter

ADAPTERS: dict[str, type[SourceAdapter]] = {
    cls.id: cls
    for cls in (
        GitHubAdapter,
        HackerNewsAdapter,
        HuggingFaceAdapter,
        ArxivAdapter,
        McpRegistryAdapter,
        DefiLlamaAdapter,
        PolymarketAdapter,
        SolanaRpcAdapter,
        RssAdapter,
        RedditAdapter,
        MastodonAdapter,
        XAdapter,
    )
}
