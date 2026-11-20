"""X recent-search adapter: off by default, read-only, charged before and after every call."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from liquidmexx.config import load_budgets, load_sources
from liquidmexx.costs.budget import BudgetGuard
from liquidmexx.db import connect, migrate
from liquidmexx.settings import Settings
from liquidmexx.sources import ADAPTERS
from liquidmexx.sources.base import CollectContext
from liquidmexx.sources.http import PoliteClient
from liquidmexx.sources.x import XAdapter, post_read_price
from liquidmexx.store import Store

T = "2026-11-20T12:00:00.000000Z"


def settings(token: bool = True, live: str | None = "enabled") -> Settings:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    s.x_bearer_token = SecretStr("test-bearer") if token else None
    s.liquidmexx_live_x = live
    return s


@pytest.fixture
def conn(tmp_path: Any) -> sqlite3.Connection:
    c = connect(tmp_path / "x.db")
    migrate(c)
    return c


def ctx(conn: sqlite3.Connection, handler: Any, **params: Any) -> CollectContext:
    guard = BudgetGuard(conn, load_budgets())
    client = PoliteClient("x", guard, min_interval_s=0, sleep=lambda _s: None,
                          transport=httpx.MockTransport(handler))
    p = {"max_results_per_query": 10, "max_queries_per_run": 3,
         "queries": {"ai": ["a", "b", "c"]}, **params}
    return CollectContext(client=client, store=Store(conn), settings=settings(), params=p,
                          fetched_at=T, last_success_at=None)


def posts(n: int, start: int = 0) -> dict[str, Any]:
    return {"data": [{"id": str(start + i), "text": f"post {i}", "author_id": "9",
                      "created_at": T, "lang": "en",
                      "public_metrics": {"like_count": i, "retweet_count": 1, "reply_count": 2,
                                         "quote_count": 0, "impression_count": 100}}
                     for i in range(n)]}


def test_shipped_config_leaves_x_off() -> None:
    assert "x" in ADAPTERS
    assert load_sources()["x"].enabled is False


def test_needs_token_and_exact_opt_in() -> None:
    a = XAdapter()
    assert a.availability(settings(token=False))[0] is False
    assert a.availability(settings(live=None))[0] is False
    assert a.availability(settings(live="true"))[0] is False
    assert a.availability(settings())[0] is True


def test_collects_posts_and_records_actual_cost(conn: sqlite3.Connection) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=posts(3, start=len(seen) * 10))

    res = XAdapter().collect(ctx(conn, handler))
    assert len(res.items) == 9 and not res.errors
    assert all(r.method == "GET" and r.url.host == "api.x.com" for r in seen)
    assert seen[0].headers["authorization"] == "Bearer test-bearer"
    assert {o.metric for o in res.observations} >= {"likes", "reposts", "views"}
    spent = BudgetGuard(conn, load_budgets()).spent_today("x")
    assert spent == pytest.approx(9 * post_read_price())


def test_daily_cap_stops_calls_before_they_are_sent(conn: sqlite3.Connection) -> None:
    guard = BudgetGuard(conn, load_budgets())
    guard.record("x", "/2/tweets/search/recent", "post_read", units=29,
                 cost_usd=29 * post_read_price())  # 0.145 of 0.15 spent; a page needs 0.05
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=posts(1))

    res = XAdapter().collect(ctx(conn, handler))
    assert calls == 0 and res.items == []
    assert any("daily cap" in e for e in res.errors)


def test_auth_failure_stops_the_run(conn: sqlite3.Connection) -> None:
    calls = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"title": "Unauthorized"})

    res = XAdapter().collect(ctx(conn, handler))
    assert calls == 1 and res.errors


def test_bearer_token_never_in_errors_or_items(conn: sqlite3.Connection) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=posts(2))

    res = XAdapter().collect(ctx(conn, handler))
    blob = json.dumps([i.__dict__ for i in res.items], default=str) + str(res.errors)
    assert "test-bearer" not in blob
