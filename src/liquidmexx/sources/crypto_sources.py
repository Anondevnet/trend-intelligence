"""Crypto sources: DeFiLlama, Polymarket (public Gamma API, read-only), Solana RPC."""

from __future__ import annotations

from typing import Any

from liquidmexx.sources.base import CollectContext, CollectResult, SourceAdapter, strip_html
from liquidmexx.sources.http import SourceHTTPError
from liquidmexx.store import Observation, RawItem
from liquidmexx.timeutil import from_unix

DEFAULT_SOLANA_RPC = "https://api.mainnet-beta.solana.com"


class DefiLlamaAdapter(SourceAdapter):
    id = "defillama"
    kind = "onchain_analytics"
    min_interval_s = 1.0

    def collect(self, ctx: CollectContext) -> CollectResult:
        result = CollectResult()
        min_tvl = float(ctx.params.get("min_tvl_usd", 1_000_000))
        protocols: list[dict[str, Any]] = ctx.client.get_json("https://api.llama.fi/protocols")
        for p in protocols:
            tvl = p.get("tvl")
            slug = p.get("slug")
            if not slug or not isinstance(tvl, (int, float)) or tvl < min_tvl:
                continue
            if (p.get("category") or "") == "CEX":
                continue  # exchange reserves are not protocol adoption
            key = f"llama:{slug}"
            chains = p.get("chains") or []
            listed = p.get("listedAt")
            result.items.append(RawItem(
                item_type="protocol",
                external_id=slug,
                title=p.get("name"),
                url=f"https://defillama.com/protocol/{slug}",
                body=" ".join(filter(None, [p.get("category"), " ".join(chains[:15]),
                                            strip_html(p.get("description"), 800)])),
                author=None,
                published_at=from_unix(listed) if isinstance(listed, (int, float)) else None,
                payload={"category": p.get("category"), "chains": chains[:30],
                         "twitter": p.get("twitter"), "gecko_id": p.get("gecko_id")},
            ))
            ref = ("protocol", slug)
            result.observations.append(Observation("protocol", key, "tvl_usd", float(tvl),
                                                   item_ref=ref))
            if isinstance(p.get("mcap"), (int, float)):
                result.observations.append(
                    Observation("protocol", key, "mcap_usd", float(p["mcap"]), item_ref=ref))
        if ctx.params.get("chains", True):
            try:
                chains_data: list[dict[str, Any]] = ctx.client.get_json(
                    "https://api.llama.fi/v2/chains")
            except SourceHTTPError as exc:
                result.errors.append(f"chains: {exc}")
                chains_data = []
            for c in chains_data:
                name, tvl = c.get("name"), c.get("tvl")
                if name and isinstance(tvl, (int, float)):
                    result.observations.append(
                        Observation("chain", f"chain:{str(name).lower()}", "tvl_usd", float(tvl)))
        return result


class PolymarketAdapter(SourceAdapter):
    """Public market metadata/volume only. Never trades, never authenticates."""

    id = "polymarket"
    kind = "prediction_market"
    min_interval_s = 1.0

    def collect(self, ctx: CollectContext) -> CollectResult:
        result = CollectResult()
        events: list[dict[str, Any]] = ctx.client.get_json(
            "https://gamma-api.polymarket.com/events",
            params={"active": "true", "closed": "false", "order": "volume24hr",
                    "ascending": "false", "limit": int(ctx.params.get("events_limit", 100))},
        )
        for e in events:
            eid = str(e.get("id") or "")
            if not eid:
                continue
            tags = [t.get("label") for t in (e.get("tags") or []) if t.get("label")]
            slug = e.get("slug") or eid
            result.items.append(RawItem(
                item_type="market",
                external_id=eid,
                title=e.get("title"),
                url=f"https://polymarket.com/event/{slug}",
                body=" ".join([" ".join(tags), strip_html(e.get("description"), 1500)]),
                published_at=e.get("createdAt"),
                payload={"slug": slug, "tags": tags, "endDate": e.get("endDate")},
            ))
            key = f"pm:{eid}"
            ref = ("market", eid)
            for metric, field in (("volume_24h_usd", "volume24hr"), ("liquidity_usd", "liquidity"),
                                  ("open_interest_usd", "openInterest"),
                                  ("comments", "commentCount")):
                val = e.get(field)
                if isinstance(val, (int, float)):
                    result.observations.append(Observation("market", key, metric, float(val),
                                                           item_ref=ref))
        return result


class SolanaRpcAdapter(SourceAdapter):
    id = "solana_rpc"
    kind = "onchain"
    min_interval_s = 2.0

    def collect(self, ctx: CollectContext) -> CollectResult:
        result = CollectResult()
        url = ctx.settings.solana_rpc_url or DEFAULT_SOLANA_RPC
        body = {"jsonrpc": "2.0", "id": 1, "method": "getRecentPerformanceSamples",
                "params": [int(ctx.params.get("performance_samples", 10))]}
        data = ctx.client.request("POST", url, json=body).json()
        if "error" in data:
            result.errors.append(f"rpc error: {data['error'].get('message', 'unknown')}")
            return result
        samples = data.get("result") or []
        secs = sum(s.get("samplePeriodSecs", 0) for s in samples)
        if secs > 0:
            tx = sum(s.get("numTransactions", 0) for s in samples)
            non_vote = sum(s.get("numNonVoteTransactions", 0) for s in samples)
            result.observations += [
                Observation("network", "solana", "tps", tx / secs),
                Observation("network", "solana", "non_vote_tps", non_vote / secs),
            ]
        return result
