"""Mastodon hashtag adapter: free, GET-only, stored but never linked to trends."""

from __future__ import annotations

import sqlite3
from typing import Any

import httpx
import pytest

from liquidmexx.config import load_budgets, load_sources
from liquidmexx.costs.budget import BudgetGuard
from liquidmexx.db import connect, migrate
from liquidmexx.settings import Settings
from liquidmexx.sources import ADAPTERS
from liquidmexx.sources.base import CollectContext
from liquidmexx.sources.http import PoliteClient
from liquidmexx.sources.mastodon import MastodonAdapter
from liquidmexx.store import Store
from liquidmexx.trends.identity import link_new_items, seed_taxonomy

T = "2026-11-20T12:00:00.000000Z"


@pytest.fixture
def conn(tmp_path: Any) -> sqlite3.Connection:
    c = connect(tmp_path / "m.db")
    migrate(c)
    return c


def status(i: int, **over: Any) -> dict[str, Any]:
    base = {"id": str(i), "created_at": T, "language": "en", "reblog": None,
            "url": f"https://mastodon.social/@a/{i}", "content": f"<p>MCP server post {i}</p>",
            "favourites_count": i, "reblogs_count": 2, "replies_count": 1, "quotes_count": 0,
            "account": {"acct": "alice", "followers_count": 50}}
    return {**base, **over}


def ctx(conn: sqlite3.Connection, handler: Any, **params: Any) -> CollectContext:
    guard = BudgetGuard(conn, load_budgets())
    client = PoliteClient("mastodon", guard, min_interval_s=0, sleep=lambda _s: None,
                          transport=httpx.MockTransport(handler))
    p = {"tags": {"ai": ["AI", "MCP"]}, "instance": "mastodon.social", **params}
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    return CollectContext(client=client, store=Store(conn), settings=s, params=p,
                          fetched_at=T, last_success_at=None)


def test_shipped_config_and_no_credentials_needed() -> None:
    assert "mastodon" in ADAPTERS
    assert load_sources()["mastodon"].enabled is True
    assert MastodonAdapter().availability(Settings(_env_file=None))[0] is True  # type: ignore[call-arg]


def test_collects_engagement_and_skips_boosts_and_other_languages(
        conn: sqlite3.Connection) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=[status(5), status(6, reblog={"id": "1"}),
                                         status(7, language="es"), status(5)])

    res = MastodonAdapter().collect(ctx(conn, handler))
    assert [i.external_id for i in res.items] == ["mastodon.social:5"]
    assert res.items[0].body == "MCP server post 5"
    assert res.items[0].author == "alice@mastodon.social"
    got = {o.metric: o.value for o in res.observations}
    assert got == {"likes": 5.0, "reposts": 2.0, "replies": 1.0, "quotes": 0.0}
    assert all(r.method == "GET" and r.url.host == "mastodon.social" for r in seen)
    assert len(seen) == 2  # one call per tag


def test_rate_limit_stops_the_run(conn: sqlite3.Connection) -> None:
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "1"})

    res = MastodonAdapter().collect(ctx(conn, handler))
    assert res.errors and len(res.items) == 0
    assert len(calls) <= 5  # bounded retries, then it stops instead of hammering every tag


def test_mastodon_posts_are_never_linked_to_trends(conn: sqlite3.Connection) -> None:
    store = Store(conn)
    store.ensure_source("mastodon", "social")
    run = store.start_run("mastodon")
    item = MastodonAdapter().collect(
        ctx(conn, lambda r: httpx.Response(200, json=[status(9)]))).items[0]
    store.insert_raw_item("mastodon", item, run, T)
    seed_taxonomy(conn, {"categories": {"ai": [{"name": "MCP", "aliases": ["mcp server"]}]}})
    assert link_new_items(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM trend_mentions").fetchone()[0] == 0


def test_mastodon_never_counts_toward_stage6_readiness(conn: sqlite3.Connection) -> None:
    from liquidmexx.detectors.comparison import live_days
    store = Store(conn)
    store.ensure_source("mastodon", "social")
    run = store.start_run("mastodon")
    first = store.start_run("mastodon")  # later runs are not the initial baseline snapshot
    for d, rid in (("2026-11-18", run), ("2026-11-19", first), ("2026-11-20", first)):
        item = MastodonAdapter().collect(
            ctx(conn, lambda r, d=d: httpx.Response(200, json=[status(int(d[-2:]))]))).items[0]
        store.insert_raw_item("mastodon", item, rid, f"{d}T10:00:00.000000Z")
    assert "mastodon" not in live_days(conn)
