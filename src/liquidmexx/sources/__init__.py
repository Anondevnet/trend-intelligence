"""Source adapter registry."""

from __future__ import annotations

from liquidmexx.sources.ai_sources import ArxivAdapter, HuggingFaceAdapter, McpRegistryAdapter
from liquidmexx.sources.base import SourceAdapter
from liquidmexx.sources.crypto_sources import DefiLlamaAdapter, PolymarketAdapter, SolanaRpcAdapter
from liquidmexx.sources.feeds import RedditAdapter, RssAdapter
from liquidmexx.sources.github import GitHubAdapter
from liquidmexx.sources.hackernews import HackerNewsAdapter
from liquidmexx.sources.mastodon import MastodonAdapter
from liquidmexx.sources.x import XAdapter

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
