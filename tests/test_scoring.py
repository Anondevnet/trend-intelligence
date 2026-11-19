"""Stage 4 scoring: baseline exclusion, clone-farm discount, point-in-time reproducibility."""

from __future__ import annotations

import inspect
import socket
import sqlite3
from datetime import timedelta
from typing import Any

import pytest

from liquidmexx.backfill import backfill_hackernews
from liquidmexx.config import load_scoring, source_credibility
from liquidmexx.scoring import Scorer, verify_run
from liquidmexx.scoring import engine as engine_mod
from liquidmexx.store import Observation, RawItem, Store
from liquidmexx.timeutil import parse_iso, to_iso

T = "2026-11-01T12:00:00.000000Z"
CRED = {"hackernews": 0.6, "rss": 0.6, "github": 0.7, "defillama": 0.9}


def at(hours: float) -> str:
    """T shifted by ``hours`` (negative = earlier)."""
    return to_iso(parse_iso(T) + timedelta(hours=hours))


def cfg(**over: Any) -> dict[str, Any]:
    c = load_scoring()
    c.update(over)
    return c


class World:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.store = Store(conn)
        self.n = 0
        self.trend = self.add_trend("ai-agents/agents")

    def add_trend(self, slug: str, kind: str = "narrative", key: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO trends(slug, name, category, kind, origin, subject_type, subject_key,"
            " created_at) VALUES (?,?,?,?,?,?,?,?)",
            (slug, slug, "x", kind, "taxonomy" if kind == "narrative" else "github",
             "repo" if key else None, key, T))
        return int(cur.lastrowid or 0)

    def run(self, source: str) -> int:
        self.store.ensure_source(source, "test")
        return self.store.start_run(source)

    def prime(self, source: str) -> None:
        """Give a source a first (baseline) run long before T, so later runs are live."""
        self.item(source, at(-24 * 60), run_id=self.run(source))

    def item(self, source: str, when: str, *, run_id: int | None = None, ext: str | None = None,
             backfill: bool = False, item_type: str = "story", trend: int | None = None) -> int:
        self.n += 1
        rid, _ = self.store.insert_raw_item(
            source, RawItem(item_type, ext or f"{source}-{self.n}", title="t"),
            run_id if run_id is not None else self.run(source), seen_at=when,
            available_at=when, is_backfill=backfill)
        if trend is not None:
            self.conn.execute(
                "INSERT INTO trend_mentions(trend_id, raw_item_id, source_id, available_at,"
                " linked_at, linker_version) VALUES (?,?,?,?,?, 'v1')",
                (trend, rid, source, when, when))
        return rid

    def points(self, source: str, ext: str, value: float, when: str) -> None:
        self.store.add_observations(source, [Observation(
            "story", ext, "points", value, observed_at=when, available_at=when,
            item_ref=("story", ext))], run_id=None, fetched_at=when)


def score_of(conn: sqlite3.Connection, trend: int, as_of: str = T, **over: Any) -> Any:
    results, excluded = Scorer(conn, cfg(**over), CRED).score(as_of)
    return {s.trend_id: s for s in results}.get(trend), excluded


def test_first_run_catalog_items_are_excluded(conn: sqlite3.Connection) -> None:
    w = World(conn)
    first = w.run("defillama")
    for _ in range(50):
        w.item("defillama", at(-1), run_id=first, trend=w.trend)
    s, excluded = score_of(conn, w.trend)
    assert s is None and excluded["initial_snapshot"] == 50
    second = w.run("defillama")
    for _ in range(2):
        w.item("defillama", at(-0.5), run_id=second, trend=w.trend)
    s, _ = score_of(conn, w.trend)
    assert s.evidence["window_sources"] == {"defillama": 2.0}


def test_hn_first_run_is_counted_by_config(conn: sqlite3.Connection) -> None:
    w = World(conn)
    first = w.run("hackernews")
    w.item("hackernews", at(-1), run_id=first, ext="h1", trend=w.trend)
    w.points("hackernews", "h1", 10, at(-1))
    assert score_of(conn, w.trend)[0] is not None
    assert score_of(conn, w.trend, first_run_counts_for=[])[0] is None


def test_hn_live_story_needs_points_known_by_as_of(conn: sqlite3.Connection) -> None:
    w = World(conn)
    w.prime("hackernews")
    w.item("hackernews", at(-2), ext="s1", trend=w.trend)
    s, ex = score_of(conn, w.trend)
    assert s is None and ex["below_engagement_gate"] == 1         # no points on record
    w.points("hackernews", "s1", 2, at(-1.5))
    assert score_of(conn, w.trend)[0] is None                      # below threshold of 3
    w.points("hackernews", "s1", 7, at(1))                         # known only after T
    assert score_of(conn, w.trend)[0] is None
    assert score_of(conn, w.trend, as_of=at(2))[0] is not None
    # Backfill stories were already filtered to >= 3 points at collection time.
    w.item("hackernews", at(-3), ext="b1", backfill=True, trend=w.trend)
    assert score_of(conn, w.trend)[0].evidence["window_sources"] == {"hackernews": 1.0}


def test_backfill_threshold_matches_scoring_gate() -> None:
    default = inspect.signature(backfill_hackernews).parameters["min_points"].default
    assert load_scoring()["engagement_gates"]["hackernews"]["min"] == default


def test_clone_farm_discount_uses_only_repos_known_at_as_of(conn: sqlite3.Connection) -> None:
    w = World(conn)
    w.prime("github")
    for owner in ("a", "b"):
        w.item("github", at(-2), ext=f"{owner}/agent-kit", item_type="repo", trend=w.trend)
    w.item("github", at(-1), ext="c/agent-kit", item_type="repo", trend=w.trend)
    w.item("github", at(-1), ext="d/real-agent", item_type="repo", trend=w.trend)
    s, ex = score_of(conn, w.trend, as_of=at(-1.5))                # only 2 owners known
    assert ex["clone_farm_discounted"] == 0
    assert s.evidence["window_sources"] == {"github": 2.0}
    s, ex = score_of(conn, w.trend)                                # 3 owners by T
    assert ex["clone_farm_discounted"] == 3
    assert s.evidence["window_sources"] == {"github": pytest.approx(1.3)}
    assert s.evidence["discounted_in_window"] == 3


def test_clone_farm_discount_applies_to_repo_entity_trends(conn: sqlite3.Connection) -> None:
    w = World(conn)
    w.prime("github")
    w.prime("hackernews")
    for owner in ("a", "b", "c"):
        w.item("github", at(-5), ext=f"{owner}/lure", item_type="repo")
    ent = w.add_trend("entity/repo/a/lure", kind="entity", key="a/lure")
    w.item("hackernews", at(-1), ext="hx", backfill=True, trend=ent)
    s, _ = score_of(conn, ent)
    assert s.evidence["window_sources"] == {"hackernews": pytest.approx(0.1)}


def test_acceleration_is_per_source_and_needs_own_baseline(conn: sqlite3.Connection) -> None:
    w = World(conn)
    w.prime("hackernews")
    w.prime("rss")
    for day in range(1, 15):                                       # steady HN baseline: 2/day
        for _ in range(2):
            w.item("hackernews", at(-24 * day - 1), backfill=True, trend=w.trend)
    for _ in range(10):
        w.item("hackernews", at(-1), backfill=True, trend=w.trend)
    rss_only = w.add_trend("solana/solana")
    for _ in range(10):
        w.item("rss", at(-1), trend=rss_only)                      # rss has no history
    hn, _ = score_of(conn, w.trend)
    assert hn.evidence["per_source"]["hackernews"]["baseline_covered_buckets"] == 14
    assert hn.evidence["per_source"]["hackernews"]["z"] > 3
    assert hn.components["acceleration"] > 0.7
    rss, _ = score_of(conn, rss_only)
    assert "z" not in rss.evidence["per_source"]["rss"]
    assert rss.components["acceleration"] is None and rss.components["novelty"] is None
    assert rss.confidence == pytest.approx(0.45)                   # volume + breadth only
    assert rss.score < hn.score


def test_few_mentions_cannot_spike(conn: sqlite3.Connection) -> None:
    w = World(conn)
    w.prime("hackernews")
    for day in range(1, 15):
        w.item("hackernews", at(-24 * day - 1), backfill=True)    # coverage, no mentions
    w.item("hackernews", at(-1), backfill=True, trend=w.trend)
    s, _ = score_of(conn, w.trend)
    assert s.evidence["per_source"]["hackernews"]["z"] <= 0
    assert s.components["acceleration"] == 0.0
    # Brand-new with coverage, but a single mention only earns 1/3 of the novelty.
    assert s.components["novelty"] == pytest.approx(1 / 3, abs=0.01)
    for _ in range(2):
        w.item("hackernews", at(-0.5), backfill=True, trend=w.trend)
    assert score_of(conn, w.trend)[0].components["novelty"] == pytest.approx(1.0, abs=0.02)


def _seed_history(w: World) -> None:
    w.prime("hackernews")
    w.prime("github")
    for day in range(0, 14):
        w.item("hackernews", at(-24 * day - 2), backfill=True, trend=w.trend)
    w.item("github", at(-3), ext="o/agent", item_type="repo", trend=w.trend)


def test_scores_do_not_change_when_later_data_arrives(conn: sqlite3.Connection) -> None:
    w = World(conn)
    _seed_history(w)
    before, _ = Scorer(conn, cfg(), CRED).score(T)
    # Later data of every kind: items, mentions, points, clone-farm siblings, new trends.
    for owner in ("p", "q", "r"):
        w.item("github", at(1), ext=f"{owner}/agent", item_type="repo", trend=w.trend)
    w.item("hackernews", at(0.5), ext="late", trend=w.trend)
    w.points("hackernews", "late", 99, at(0.6))
    w.item("rss", at(2), trend=w.add_trend("defi/defi"))
    after, _ = Scorer(conn, cfg(), CRED).score(T)
    assert [(s.trend_id, s.score, s.components, s.evidence) for s in before] == [
        (s.trend_id, s.score, s.components, s.evidence) for s in after]


def test_save_verify_and_append_only(conn: sqlite3.Connection) -> None:
    w = World(conn)
    _seed_history(w)
    scorer = Scorer(conn, cfg(), CRED)
    results, _ = scorer.score(T)
    run_id = scorer.save(T, results)
    assert run_id is not None
    assert scorer.save(T, results) is None                         # same run not duplicated
    w.item("hackernews", at(5), backfill=True, trend=w.trend)      # later data
    assert verify_run(conn, run_id) == []
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE trend_scores SET score = 0")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM scoring_runs")
    # A changed config is a different, separately stored run (different hash).
    other = Scorer(conn, cfg(volume_scale=10.0), CRED)
    assert other.hash != scorer.hash
    assert other.save(T, other.score(T)[0]) is not None


def test_scoring_makes_no_network_calls(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: Any, **__: Any) -> None:
        raise AssertionError("network access during scoring")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    w = World(conn)
    _seed_history(w)
    assert Scorer(conn, cfg(), CRED).score(T)[0]
    src = inspect.getsource(engine_mod)
    assert "httpx" not in src and "anthropic" not in src


def test_real_config_loads_and_is_hashed_with_credibility() -> None:
    c = load_scoring()
    assert sum(c["weights"].values()) == pytest.approx(1.0)
    cred = source_credibility()
    a = Scorer(sqlite3.connect(":memory:"), c, cred).hash
    cred2 = dict(cred, hackernews=0.1)
    assert Scorer(sqlite3.connect(":memory:"), c, cred2).hash != a
