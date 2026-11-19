"""Adapter + collector tests. All HTTP is mocked with respx; nothing reaches the network."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import httpx
import pytest
import respx

from liquidmexx.backfill import daily_cumulative
from liquidmexx.collector import is_due, persist, run_source
from liquidmexx.config import SourceConfig, load_budgets, load_sources
from liquidmexx.costs.budget import BudgetGuard
from liquidmexx.settings import Settings
from liquidmexx.sources.base import CollectResult
from liquidmexx.sources.http import PoliteClient, SourceHTTPError
from liquidmexx.store import Observation, RawItem, RunStats, Store
from liquidmexx.timeutil import now_iso
from tests.conftest import no_sleep

ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <entry><id>http://arxiv.org/abs/2611.01234v2</id><published>2026-11-18T10:00:00Z</published>
  <title>Agentic Coding with MCP Servers</title><summary>We study ai agents using
  model context protocol servers.</summary><author><name>A. Author</name></author>
  <category term="cs.AI"/></entry>
</feed>"""

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Solana DEX volume hits record</title><link>https://ex.com/a</link>
<guid>a1</guid><pubDate>Wed, 18 Nov 2026 12:00:00 GMT</pubDate>
<description>&lt;p&gt;Perp DEX activity on &lt;b&gt;Solana&lt;/b&gt;&lt;/p&gt;</description></item>
</channel></rss>"""


def gh_repo(name: str, stars: int) -> dict[str, object]:
    return {"full_name": name, "html_url": f"https://github.com/{name}", "description": "ai agent",
            "topics": ["llm"], "owner": {"login": name.split("/")[0]},
            "created_at": "2026-11-15T00:00:00Z", "stargazers_count": stars, "forks_count": 3,
            "open_issues_count": 1}


def run(conn: sqlite3.Connection, sid: str, params: dict[str, object] | None = None,
        settings: Settings | None = None) -> tuple[str, RunStats]:
    cfg = SourceConfig(enabled=True, params=params if params is not None
                       else load_sources()[sid].params)
    report = run_source(conn, sid, cfg, settings=settings or Settings(_env_file=None),  # type: ignore[call-arg]
                        sleep=no_sleep)
    return report.status, report.stats


@respx.mock
def test_github_search_and_refresh(conn: sqlite3.Connection) -> None:
    respx.get("https://api.github.com/search/repositories").mock(
        return_value=httpx.Response(200, json={"items": [gh_repo("Acme/Agent-X", 120)]}))
    status, stats = run(conn, "github", {"queries": {"ai": ["topic:llm"]}, "min_stars": 10})
    assert status == "ok" and stats.items_new == 1
    store = Store(conn)
    assert store.value_as_of("repo", "acme/agent-x", "stars", now_iso()) == 120.0
    # Second run: no search hits -> repo is re-measured via /repos (velocity).
    respx.get("https://api.github.com/search/repositories").mock(
        return_value=httpx.Response(200, json={"items": []}))
    respx.get("https://api.github.com/repos/acme/agent-x").mock(
        return_value=httpx.Response(200, json=gh_repo("Acme/Agent-X", 180)))
    status, stats = run(conn, "github", {"queries": {"ai": ["topic:llm"]}})
    assert status == "ok" and stats.items_new == 0
    assert store.value_as_of("repo", "acme/agent-x", "stars", now_iso()) == 180.0


@respx.mock
def test_github_token_sent_as_header_and_never_logged(conn: sqlite3.Connection) -> None:
    token = "ghp_" + "T" * 36
    route = respx.get("https://api.github.com/search/repositories").mock(
        return_value=httpx.Response(500, json={"message": f"boom {token}"}))
    s = Settings(_env_file=None, github_token=token)  # type: ignore[call-arg]
    status, _ = run(conn, "github", {"queries": {"ai": ["topic:llm"]}}, settings=s)
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {token}"
    assert status == "partial"
    errors = conn.execute("SELECT error FROM collection_runs").fetchone()[0]
    assert token not in errors


@respx.mock
def test_hackernews_items_and_points(conn: sqlite3.Connection) -> None:
    hit = {"objectID": "777", "title": "Show HN: an MCP server for X", "url": "https://github.com/a/b",
           "author": "pg", "created_at": "2026-11-19T10:00:00Z", "points": 42, "num_comments": 7}
    respx.get("https://hn.algolia.com/api/v1/search_by_date").mock(
        return_value=httpx.Response(200, json={"hits": [hit], "nbPages": 1}))
    status, stats = run(conn, "hackernews")
    assert status == "ok" and stats.items_new == 1
    assert Store(conn).value_as_of("story", "hn:777", "points", now_iso()) == 42.0


@respx.mock
def test_arxiv_rss_hf_polymarket_defillama_solana_mcp(conn: sqlite3.Connection) -> None:
    respx.get("https://export.arxiv.org/api/query").mock(return_value=httpx.Response(200,
                                                                                     text=ARXIV_ATOM))
    respx.get(url__regex=r"https://(www\.)?(coindesk|theblock|blockworks|decrypt|thedefiant|solana|"
                         r"news\.polymarket|huggingface\.co/blog|openai|blog\.google|simonwillison|"
                         r"latent).*").mock(return_value=httpx.Response(200, text=RSS))
    respx.get("https://huggingface.co/api/models").mock(return_value=httpx.Response(200, json=[
        {"id": "Org/Model-7B", "likes": 50, "downloads": 900, "trendingScore": 30,
         "createdAt": "2026-11-17T00:00:00Z", "tags": ["text-generation"]}]))
    respx.get("https://huggingface.co/api/spaces").mock(return_value=httpx.Response(200, json=[]))
    respx.get("https://gamma-api.polymarket.com/events").mock(return_value=httpx.Response(
        200, json=[
        {"id": "9", "slug": "will-x", "title": "Will X?", "volume24hr": 1000.5, "liquidity": 50,
         "createdAt": "2026-11-01T00:00:00Z", "tags": [{"label": "Crypto"}]}]))
    respx.get("https://api.llama.fi/protocols").mock(return_value=httpx.Response(200, json=[
        {"slug": "big", "name": "Big", "tvl": 5e8, "category": "Dexs", "chains": ["Solana"],
         "listedAt": 1700000000},
        {"slug": "tiny", "name": "Tiny", "tvl": 10.0},
        {"slug": "cex", "name": "Cex", "tvl": 9e9, "category": "CEX"}]))
    respx.get("https://api.llama.fi/v2/chains").mock(return_value=httpx.Response(200, json=[
        {"name": "Solana", "tvl": 9e9}]))
    respx.post("https://api.mainnet-beta.solana.com").mock(return_value=httpx.Response(200, json={
        "result": [{"numTransactions": 6000, "numNonVoteTransactions": 3000,
                    "samplePeriodSecs": 60}]}))
    respx.get("https://registry.modelcontextprotocol.io/v0/servers").mock(
        return_value=httpx.Response(200, json={"servers": [{"server": {
            "name": "io.github.a/b", "description": "MCP server"}, "_meta": {}}],
            "metadata": {"count": 1}}))

    for sid in ("arxiv", "rss", "huggingface", "polymarket", "defillama", "solana_rpc",
                "mcp_registry"):
        status, stats = run(conn, sid)
        assert status == "ok", (sid, stats.errors)

    store = Store(conn)
    t = now_iso()
    assert store.raw_item_id("arxiv", "paper", "2611.01234") is not None
    assert store.value_as_of("model", "org/model-7b", "likes", t) == 50.0
    assert store.value_as_of("market", "pm:9", "volume_24h_usd", t) == 1000.5
    assert store.value_as_of("protocol", "llama:big", "tvl_usd", t) == 5e8
    assert store.raw_item_id("defillama", "protocol", "tiny") is None   # below min TVL
    assert store.raw_item_id("defillama", "protocol", "cex") is None    # CEX excluded
    assert store.value_as_of("network", "solana", "tps", t) == 100.0
    body = conn.execute("SELECT body FROM raw_items WHERE source_id='rss' LIMIT 1").fetchone()[0]
    assert "<" not in body  # HTML stripped


def test_reddit_disabled_without_credentials(conn: sqlite3.Connection) -> None:
    status, _ = run(conn, "reddit", {"subreddits": ["solana"]})
    assert status == "skipped"


@respx.mock
def test_retry_on_429_then_success(conn: sqlite3.Connection) -> None:
    route = respx.get("https://hn.algolia.com/api/v1/search_by_date")
    route.side_effect = [httpx.Response(429, headers={"retry-after": "1"}),
                         httpx.Response(200, json={"hits": [], "nbPages": 0}),
                         httpx.Response(200, json={"hits": []})]
    status, stats = run(conn, "hackernews")
    assert status == "ok" and stats.requests_made == 3


def test_non_https_refused(conn: sqlite3.Connection) -> None:
    g = BudgetGuard(conn, load_budgets())
    with PoliteClient("t", g, sleep=no_sleep) as c, pytest.raises(SourceHTTPError, match="HTTPS"):
        c.get_json("http://example.com/x")


@respx.mock
def test_error_run_is_recorded_not_raised(conn: sqlite3.Connection) -> None:
    respx.get("https://api.llama.fi/protocols").mock(return_value=httpx.Response(404))
    status, stats = run(conn, "defillama")
    assert status == "error" and stats.errors
    row = conn.execute("SELECT status FROM collection_runs").fetchone()
    assert row[0] == "error"


def test_persist_is_atomic(conn: sqlite3.Connection) -> None:
    store = Store(conn)
    store.ensure_source("hackernews", "c")
    run_id = store.start_run("hackernews")
    bad = CollectResult(items=[RawItem("story", "1", title="ok")],
                        observations=[Observation("story", "hn:1", "points", 1,
                                                  observed_at="2099-01-01T00:00:00Z")])
    with pytest.raises(ValueError):
        persist(store, "hackernews", bad, run_id, now_iso(), stats=RunStats())
    assert conn.execute("SELECT COUNT(*) FROM raw_items").fetchone()[0] == 0


def test_backfill_persist_skips_undated_items(conn: sqlite3.Connection) -> None:
    store = Store(conn)
    store.ensure_source("hackernews", "c")
    run_id = store.start_run("hackernews", mode="backfill")
    res = CollectResult(items=[RawItem("story", "1", title="dated",
                                       published_at="2026-10-01T00:00:00Z"),
                               RawItem("story", "2", title="undated")])
    persist(store, "hackernews", res, run_id, now_iso(), is_backfill=True, stats=RunStats())
    rows = conn.execute("SELECT external_id, available_at, is_backfill FROM raw_items").fetchall()
    assert [tuple(r) for r in rows] == [("1", "2026-10-01T00:00:00.000000Z", 1)]


def test_is_due(conn: sqlite3.Connection) -> None:
    store = Store(conn)
    store.ensure_source("rss", "news")
    cfg = SourceConfig(enabled=True, interval_minutes=60)
    assert is_due(store, "rss", cfg, now_iso())
    rid = store.start_run("rss")
    store.finish_run(rid, "ok", RunStats())
    assert not is_due(store, "rss", cfg, now_iso())


def test_daily_cumulative_star_history() -> None:
    stamps = ["2026-11-01T05:00:00Z", "2026-11-01T09:00:00Z", "2026-11-03T00:00:01Z"]
    out = daily_cumulative(stamps, datetime(2026, 11, 4, 12, tzinfo=UTC))
    assert out == [("2026-11-01T23:59:59.000000Z", 2), ("2026-11-02T23:59:59.000000Z", 2),
                   ("2026-11-03T23:59:59.000000Z", 3)]
    # today (incomplete day) is never included
    assert daily_cumulative(["2026-11-04T01:00:00Z"], datetime(2026, 11, 4, 12, tzinfo=UTC)) == []
