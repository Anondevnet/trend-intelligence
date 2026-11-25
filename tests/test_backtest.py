"""Stage 5 backtester: label leakage guards, embargoed walk-forward, determinism."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

import pytest

from tests.test_scoring import CRED, World, at, cfg
from trend_intelligence.backtest import Backtester, spearman
from trend_intelligence.config import load_backtest
from trend_intelligence.timeutil import parse_iso


def bt_cfg(**over: Any) -> dict[str, Any]:
    c = load_backtest()
    c.update(over)
    return c


def make(conn: sqlite3.Connection, **over: Any) -> Backtester:
    return Backtester(conn, cfg(), CRED, bt_cfg(**over))


def hn_days(w: World, first: int, last: int, *, trend: int | None = None, per_day: int = 1,
            hour: float = 6.0) -> None:
    """Backfilled HN items on each day offset in [first, last] (relative to T)."""
    for day in range(first, last + 1):
        for i in range(per_day):
            w.item("hackernews", at(24 * day + hour + i * 0.01), backfill=True, trend=trend)


def world_with_coverage(conn: sqlite3.Connection, days_after: int = 3) -> World:
    w = World(conn)
    w.item("hackernews", at(-24 * 21), run_id=w.run("hackernews"))   # first (baseline) run
    hn_days(w, -20, days_after)                  # HN collecting every day, no trend mentions
    return w


def test_label_uses_only_the_outcome_window(conn: sqlite3.Connection) -> None:
    w = world_with_coverage(conn)
    hn_days(w, -14, -1, trend=w.trend)           # baseline 1/day
    for _ in range(5):
        w.item("hackernews", at(12), backfill=True, trend=w.trend)    # inside (T, T+1d]
    bt = make(conn)
    lab = bt.labels(at(0), 1).labels[w.trend]
    score_before = bt.scored(at(0))[w.trend]
    assert lab[0] == 1 and lab[1] == 5.0
    # Data after T+H must not change the label; nothing after T may change the score at T.
    for _ in range(20):
        w.item("hackernews", at(30), backfill=True, trend=w.trend)
    fresh = make(conn)
    assert fresh.labels(at(0), 1).labels[w.trend] == lab
    assert fresh.scored(at(0))[w.trend] == score_before


def test_label_is_negative_without_lift(conn: sqlite3.Connection) -> None:
    w = world_with_coverage(conn)
    hn_days(w, -14, 1, trend=w.trend, per_day=3)  # steady 3/day before and after T
    assert make(conn).labels(at(0), 1).labels[w.trend][0] == 0


def test_source_switching_on_cannot_look_like_emergence(conn: sqlite3.Connection) -> None:
    w = world_with_coverage(conn)
    w.prime("rss")
    rss_trend = w.add_trend("defi/defi")
    for _ in range(10):
        w.item("rss", at(12), trend=rss_trend)   # rss has no history before T
    dl = make(conn).labels(at(0), 1)
    assert dl.label_sources == ["hackernews"]
    assert rss_trend not in dl.labels and dl.unseen_emergers == 0


def test_unseen_emergers_are_counted_not_scored(conn: sqlite3.Connection) -> None:
    w = world_with_coverage(conn)
    new = w.add_trend("ai-agents/new-thing")
    for _ in range(4):
        w.item("hackernews", at(10), backfill=True, trend=new)
    dl = make(conn).labels(at(0), 1)
    assert new not in dl.labels and dl.unseen_emergers == 1


def test_walk_forward_trains_only_on_labels_known_by_test_date(conn: sqlite3.Connection) -> None:
    w = world_with_coverage(conn, days_after=20)
    hn_days(w, -14, 20, trend=w.trend, per_day=1)
    for day in range(-6, 12, 3):                 # periodic bursts -> some positive labels
        for _ in range(6):
            w.item("hackernews", at(24 * day + 8), backfill=True, trend=w.trend)
    bt = make(conn, walk_forward={"min_train_dates": 2, "min_train_positives": 0,
                                  "threshold_quantiles": [0.5, 0.9]})
    for h in (1, 7):
        res = bt.run(h, ["scorer:s2"])[0]
        wf = res.metrics["walk_forward"]
        assert wf["status"] == "ok"
        for entry in wf["thresholds"]:
            t = parse_iso(entry["test"])
            allowed = [d for d in res.dates
                       if d < entry["test"] and parse_iso(d) + timedelta(days=h) <= t]
            assert entry["train_dates"] == len(allowed)


def test_runs_are_deterministic_and_append_only(conn: sqlite3.Connection) -> None:
    w = world_with_coverage(conn, days_after=10)
    hn_days(w, -14, 10, trend=w.trend, per_day=2)
    a = make(conn).run(1)
    b = make(conn).run(1)
    assert [json.dumps(r.metrics, sort_keys=True) for r in a] == [
        json.dumps(r.metrics, sort_keys=True) for r in b]
    assert [r.detector for r in a] == ["scorer:s2", "volume_only", "random"]
    bt = make(conn)
    run_id = bt.save(a[0])
    n = conn.execute("SELECT COUNT(*) FROM backtest_points WHERE run_id=?", (run_id,)).fetchone()
    assert n[0] == len(a[0].points) > 0
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE backtest_points SET label = 1")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM backtest_runs")


def test_long_horizon_reports_insufficient_history(conn: sqlite3.Connection) -> None:
    w = world_with_coverage(conn, days_after=3)
    hn_days(w, -14, 3, trend=w.trend)
    res = make(conn).run(30)
    assert all(r.metrics["status"] == "insufficient_history" for r in res)
    assert all(r.dates == [] for r in res)


def test_eval_dates_leave_room_for_history_and_outcome(conn: sqlite3.Connection) -> None:
    world_with_coverage(conn, days_after=5)
    bt = make(conn)
    dates = bt.eval_dates(2)
    first_data = conn.execute("SELECT MIN(available_at) FROM raw_items").fetchone()[0]
    assert parse_iso(dates[0]) >= parse_iso(first_data) + timedelta(days=8)
    assert parse_iso(dates[-1]) + timedelta(days=2) <= parse_iso(bt.data_watermark())
    assert all(d.endswith("T12:00:00.000000Z") for d in dates)


def test_spearman() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 1, 1], [1, 2, 3]) is None
    assert spearman([1, 2], [1, 2]) is None


def test_walk_forward_skips_training_sets_without_points(conn: sqlite3.Connection) -> None:
    w = World(conn)
    w.item("hackernews", at(-24 * 60), run_id=w.run("hackernews"))  # long gap, no coverage
    hn_days(w, -20, 10)
    hn_days(w, -14, 10, trend=w.trend)
    bt = make(conn, walk_forward={"min_train_dates": 1, "min_train_positives": 0,
                                  "threshold_quantiles": [0.5]})
    res = bt.run(1, ["scorer:s2"])[0]          # early dates have empty universes
    assert res.metrics["status"] == "ok"
