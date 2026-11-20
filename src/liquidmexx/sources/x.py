"""X (Twitter) recent search via the official API, read-only and budget-capped.

Off by default. It needs BOTH an X bearer token AND ``LIQUIDMEXX_LIVE_X=enabled`` in the
environment, and ``enabled: true`` for ``x`` in config/sources.yaml. Every request is checked
against the daily X cap *before* it is sent, using the worst-case cost of the page requested, and
the real cost (posts returned x unit price) is recorded after. There is no write call here.
"""

from __future__ import annotations

from typing import Any

from liquidmexx.config import load_pricing
from liquidmexx.costs.budget import BudgetExceededError
from liquidmexx.settings import Settings
from liquidmexx.sources.base import CollectContext, CollectResult, SourceAdapter
from liquidmexx.sources.http import SourceHTTPError
from liquidmexx.store import Observation, RawItem

SEARCH_URL = "https://api.x.com/2/tweets/search/recent"
ENDPOINT = "/2/tweets/search/recent"
METRICS = (("like_count", "likes"), ("retweet_count", "reposts"), ("reply_count", "replies"),
           ("quote_count", "quotes"), ("impression_count", "views"))


def post_read_price() -> float:
    return float(load_pricing()["x"]["unit_usd"]["post_read"])


def post_item(post: dict[str, Any]) -> RawItem:
    pid = str(post["id"])
    return RawItem(
        item_type="post",
        external_id=pid,
        title=(post.get("text") or "")[:200],
        url=f"https://x.com/i/web/status/{pid}",
        body=(post.get("text") or "")[:1000],
        author=post.get("author_id"),
        published_at=post.get("created_at"),
        payload={"lang": post.get("lang")},
    )


def post_observations(post: dict[str, Any]) -> list[Observation]:
    pm = post.get("public_metrics") or {}
    key, ref = f"x:{post['id']}", ("post", str(post["id"]))
    return [Observation("post", key, name, float(pm[field]), item_ref=ref)
            for field, name in METRICS if field in pm]


class XAdapter(SourceAdapter):
    id = "x"
    kind = "social"
    min_interval_s = 1.5

    def availability(self, settings: Settings) -> tuple[bool, str]:
        if not settings.x_bearer_token:
            return False, "needs X_BEARER_TOKEN"
        if not settings.live_x_opt_in:
            return False, "needs LIQUIDMEXX_LIVE_X=enabled"
        return True, "bearer token present and live X reads opted in"

    def collect(self, ctx: CollectContext) -> CollectResult:
        result = CollectResult()
        token = ctx.settings.x_bearer_token
        assert token is not None
        headers = {"Authorization": f"Bearer {token.get_secret_value()}"}
        p = ctx.params
        per_query = min(max(int(p.get("max_results_per_query", 10)), 10), 100)
        max_queries = int(p.get("max_queries_per_run", 3))
        suffix = str(p.get("query_suffix", "-is:retweet lang:en"))
        price = post_read_price()
        queries = [q for group in (p.get("queries") or {}).values() for q in group][:max_queries]
        seen: set[str] = set()
        for q in queries:
            try:  # worst case: a full page of fresh posts. Refuse before sending if it can't fit.
                ctx.client.guard.check("x", per_query * price)
            except BudgetExceededError as exc:
                result.errors.append(f"x: daily cap reached, stopped early ({exc})")
                break
            try:
                data = ctx.client.get_json(
                    SEARCH_URL, headers=headers, operation="post_read",
                    params={"query": f"{q} {suffix}".strip(), "max_results": per_query,
                            "tweet.fields": "created_at,public_metrics,author_id,lang"})
            except SourceHTTPError as exc:
                result.errors.append(f"x: {exc}")
                if exc.status_code in (401, 402, 403):
                    break  # auth or credits problem: more calls won't help
                continue
            posts = data.get("data") or []
            ctx.client.guard.record("x", ENDPOINT, "post_read", units=len(posts),
                                    cost_usd=len(posts) * price, run_id=ctx.client.run_id,
                                    detail=f"query={q[:60]}")
            for post in posts:
                if str(post["id"]) in seen:
                    continue
                seen.add(str(post["id"]))
                result.items.append(post_item(post))
                result.observations.extend(post_observations(post))
        return result
