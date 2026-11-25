"""Draft composer: facts from our own data, style from engaged posts, queue only, never posts."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_scoring import T, at
from trend_intelligence.config import load_publishing
from trend_intelligence.dashboard.data import connect_readonly, drafts_view
from trend_intelligence.dashboard.render import drafts_page
from trend_intelligence.dashboard.server import load_config, render_path
from trend_intelligence.db import connect, migrate
from trend_intelligence.publishing import status_of
from trend_intelligence.publishing.composer import compose, hook_style, save
from trend_intelligence.store import Observation, RawItem, Store
from trend_intelligence.trends.identity import seed_taxonomy

TAXO = {"categories": {"ai": [{"name": "Agent payments", "aliases": ["agent payments"]}]}}


def vel(stage: str = "Accelerating", n24: int = 12, g: float | None = 1.5) -> Any:
    return SimpleNamespace(slug="ai/agent-payments", stage=stage, n24=n24, sources=3, g24h=g)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = connect(tmp_path / "c.db")
    migrate(c)
    seed_taxonomy(c, TAXO)
    return c


def post(conn: sqlite3.Connection, i: int, text: str, likes: int,
         source: str = "mastodon") -> int:
    store = Store(conn)
    store.ensure_source(source, "social")
    run = store.start_run(source)
    rid, _ = store.insert_raw_item(source, RawItem(
        "post", f"m.s:{i}", title=text, body=text, url=f"https://m.s/@a/{i}",
        published_at=at(-2)), run, at(-2), available_at=at(-2))
    store.add_observations(source, [Observation(
        "post", f"mastodon:m.s:{i}", "likes", float(likes), observed_at=at(-0.5))], run, at(-0.5))
    return rid


def test_hook_style_reads_the_opening_shape() -> None:
    assert hook_style("Are agent payments the future?") == "question"
    assert hook_style("3 teams just shipped agent payments") == "number"
    assert hook_style("Agent payments are live") == "statement"


def test_style_follows_the_most_engaged_posts_without_copying_them(
        conn: sqlite3.Connection) -> None:
    post(conn, 1, "Are agent payments the real unlock for autonomous software?", 40)
    post(conn, 2, "Agent payments shipped today", 3)
    post(conn, 3, "Another agent payments note", 2)
    out = compose(conn, load_publishing(), [vel()], T, limit=3)
    assert len(out) == 1
    c = out[0]
    assert c.style == "question" and c.text.startswith("What's driving Agent payments?")
    assert "real unlock" not in c.text  # exemplar wording is never reused
    assert "45 likes, reposts and replies on 3 public Mastodon posts" in c.text
    assert "12 mentions across 3 sources" in c.text and len(c.text) <= 280
    assert all("raw_item_id" in s for s in c.sources) and c.basis["exemplars"][0][
        "engagement"] == 40


def test_nothing_is_suggested_when_nothing_is_rising_or_engaging(
        conn: sqlite3.Connection) -> None:
    post(conn, 1, "agent payments", 1)
    assert compose(conn, load_publishing(), [vel("Cooling")], T) == []


def test_save_queues_for_review_and_never_approves(conn: sqlite3.Connection) -> None:
    for i in range(3):
        post(conn, i, f"agent payments {i}", 12)
    cfg = load_publishing()
    ids = save(conn, cfg, compose(conn, cfg, [vel()], T), now=T)
    assert [status_of(conn, i) for i in ids] == ["pending_review"]
    assert conn.execute("SELECT COUNT(*) FROM post_events WHERE event IN"
                        " ('approved','published_dry_run','published_live')"
                        ).fetchone()[0] == 0
    assert compose(conn, cfg, [vel()], T) == []  # already drafted in the last 24h


def test_gate_failures_are_skipped(conn: sqlite3.Connection) -> None:
    for i in range(3):
        post(conn, i, f"agent payments {i}", 12)
    seed_taxonomy(conn, {"categories": {"ai": [{"name": "Agent payments", "aliases": ["x"]}]}})
    conn.execute("UPDATE trends SET name='Agent payments $SOL' WHERE slug='ai/agent-payments'")
    out = compose(conn, load_publishing(), [vel()], T)
    assert out == []  # a $cashtag in the text is blocked by the gate, so nothing is suggested


def test_post_creator_page_is_read_only_and_escaped(tmp_path: Path) -> None:
    path = tmp_path / "d.db"
    c = connect(path)
    migrate(c)
    seed_taxonomy(c, TAXO)
    for i in range(3):
        post(c, i, f"agent payments {i}", 12)
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    ro = connect_readonly(path)
    try:
        status, _ctype, body = render_path("/drafts", ro, load_config(), T)
        view = drafts_view(ro, load_config(), T)
    finally:
        ro.close()
    assert status == 200 and "Post Creator" in body and "<textarea readonly" in body
    assert "<script" not in body.lower() and "<form" not in body.lower()
    assert "posting mode" in body and isinstance(view.suggested, list)
    assert "<script" not in drafts_page(view, T).lower()
