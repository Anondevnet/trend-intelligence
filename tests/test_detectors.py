"""Stage 6 detectors: hotlist, signal_sum, ensemble weights, comparison verdicts.

Offline and synthetic only. The real comparison is not run here (it waits for 7+ live days).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest

from liquidmexx import cli
from liquidmexx.backtest import Backtester
from liquidmexx.config import load_detectors
from liquidmexx.detectors import (
    BASELINE,
    BEATS,
    INSUFFICIENT,
    NOT_BETTER,
    combine,
    compare,
    hotlist,
    member_weights,
    readiness,
    sign_test_p,
    signal_sum,
)
from liquidmexx.detectors.core import percentile_ranks, signal_points, top_k_edge
from liquidmexx.scoring.engine import Mention
from liquidmexx.timeutil import parse_iso
from tests.test_backtest import bt_cfg, hn_days, world_with_coverage
from tests.test_scoring import CRED, World, at, cfg

DCFG = load_detectors()


def m(trend: int, item: int, source: str = "hackernews", weight: float = 1.0) -> Mention:
    return Mention(trend, item, source, at(-1), weight)


def eng(source: str, metric: str, values: dict[int, float]) -> dict[str, dict[str, dict[int,
                                                                                     float]]]:
    return {source: {metric: values}}


# ------------------------------------------------------------------ config


def test_shipped_detector_config_is_valid() -> None:
    assert DCFG["comparison"]["baseline"] == "volume_only"
    assert DCFG["comparison"]["min_live_days"] >= 7
    assert "scorer" in DCFG["ensemble"]["members"]


def test_detector_config_rejects_bad_weights(tmp_path: Any) -> None:
    import yaml

    from liquidmexx.settings import PROJECT_ROOT
    bad = yaml.safe_load((PROJECT_ROOT / "config" / "detectors.yaml").read_text())
    bad["hotlist"]["weights"]["hotness"] = 0.9
    (tmp_path / "detectors.yaml").write_text(yaml.safe_dump(bad))
    with pytest.raises(ValueError, match="sum to 1"):
        load_detectors(tmp_path)


# ------------------------------------------------------------------ hotlist


def test_hotlist_prefers_top_ranked_items() -> None:
    mentions = [m(1, 10), m(2, 11), m(3, 12)]
    scores = hotlist(mentions, eng("hackernews", "points", {10: 500, 11: 50, 12: 5}), DCFG)
    assert scores[1] > scores[2] > scores[3]


def test_hotlist_frequency_is_capped() -> None:
    many = [m(1, i) for i in range(30)]
    some = [m(1, i) for i in range(10)]
    e = eng("hackernews", "points", {})
    assert hotlist(many, e, DCFG)[1] == hotlist(some, e, DCFG)[1]


def test_hotlist_sources_without_metric_rank_last() -> None:
    scores = hotlist([m(1, 10, "rss"), m(2, 11)],
                     eng("hackernews", "points", {11: 3}), DCFG)
    assert scores[2] > scores[1]


def test_hotlist_respects_clone_farm_weight() -> None:
    e = eng("github", "stars", {10: 100, 11: 100})
    full = hotlist([m(1, 10, "github")], e, DCFG)[1]
    farm = hotlist([m(2, 11, "github", weight=0.1)], e, DCFG)[2]
    assert farm < full


# ---------------------------------------------------------------- signal_sum


def test_signal_points_tiers_and_flat_rules() -> None:
    assert signal_points("github", 60, DCFG) == 2.0
    assert signal_points("github", 12, DCFG) == 1.0
    assert signal_points("github", None, DCFG) == 0.5
    assert signal_points("rss", None, DCFG) == 1.5
    assert signal_points("arxiv", None, DCFG) == 1.0


def test_signal_sum_applies_category_weight() -> None:
    mentions = [m(1, 10, "rss"), m(2, 11, "rss")]
    scores = signal_sum(mentions, {}, {1: "ai-agents", 2: "defi"}, DCFG)
    assert scores[1] == pytest.approx(1.5 * 1.2) and scores[2] == pytest.approx(1.5)


# ---------------------------------------------------------------- ensemble math


def test_percentile_ranks_handle_ties_and_unscored() -> None:
    p = percentile_ranks({1: 5.0, 2: 5.0, 3: 1.0}, [1, 2, 3, 4])
    assert p[1] == p[2] > p[3] > p[4]


def test_combine_ignores_zero_weight_members() -> None:
    a, b = {1: 1.0, 2: 0.0}, {1: 0.0, 2: 1.0}
    assert combine({"a": a, "b": b}, {"a": 1.0, "b": 0.0}) == combine({"a": a}, {"a": 1.0})
    assert combine({"a": a}, {"a": 0.0}) == {}


def test_top_k_edge() -> None:
    labels = {1: 1, 2: 0, 3: 0, 4: 0}
    assert top_k_edge({1: 9.0}, labels, 1) == pytest.approx(0.75)
    assert top_k_edge({2: 9.0}, labels, 1) == pytest.approx(-0.25)


def test_member_weights_earned_floored_and_fallbacks() -> None:
    labels = {1: 1, 2: 0, 3: 0, 4: 0}
    good, bad = {1: 9.0}, {2: 9.0}
    hist = [({"good": good, "bad": bad}, labels)] * 5
    w, status = member_weights(hist, ["good", "bad"], 1, 5)
    assert status == "earned" and w["good"] > 0 and w["bad"] == 0
    assert member_weights(hist[:4], ["good", "bad"], 1, 5) == (
        {"good": 1.0, "bad": 1.0}, "equal_no_history")
    only_bad = [({"bad": bad}, labels)] * 5
    assert member_weights(only_bad, ["bad"], 1, 5)[1] == "equal_no_edge"


# ------------------------------------------------- inside the backtester


def detector_world(conn: sqlite3.Connection) -> World:
    w = world_with_coverage(conn, days_after=12)
    hn_days(w, -14, 12, trend=w.trend, per_day=1)
    for day in range(-6, 10, 3):
        for _ in range(5):
            w.item("hackernews", at(24 * day + 8), backfill=True, trend=w.trend)
    return w


def make(conn: sqlite3.Connection, **over: Any) -> Backtester:
    return Backtester(conn, cfg(), CRED, bt_cfg(**over), detectors=DCFG)


def test_without_detector_config_only_the_original_three_run(conn: sqlite3.Connection) -> None:
    detector_world(conn)
    bt = Backtester(conn, cfg(), CRED, bt_cfg())
    assert list(bt.rankers(1)) == ["scorer:s2", "volume_only", "random"]


def test_all_detectors_run_deterministically(conn: sqlite3.Connection) -> None:
    detector_world(conn)
    a = make(conn).run(1)
    b = make(conn).run(1)
    assert [r.detector for r in a] == ["scorer:s2", "volume_only", "random", "hotlist",
                                       "signal_sum", "ensemble"]
    assert [r.metrics for r in a] == [r.metrics for r in b]


def test_detectors_are_point_in_time(conn: sqlite3.Connection) -> None:
    w = detector_world(conn)
    other = w.add_trend("defi/defi")
    ext = "hn-late"
    w.item("hackernews", at(-2), backfill=True, trend=other, ext=ext)
    bt = make(conn)
    before = {n: bt.rankers(1)[n](at(0)) for n in ("hotlist", "signal_sum")}
    # Engagement and mentions that arrive after T must not change scores at T.
    w.points("hackernews", ext, 900, at(3))
    for _ in range(10):
        w.item("hackernews", at(5), backfill=True, trend=other)
    fresh = make(conn)
    assert {n: fresh.rankers(1)[n](at(0)) for n in ("hotlist", "signal_sum")} == before


def test_ensemble_trains_only_on_outcomes_known_by_t(conn: sqlite3.Connection) -> None:
    detector_world(conn)
    bt = make(conn)
    for h in (1, 7):
        res = {r.detector: r for r in bt.run(h, ["ensemble"])}["ensemble"]
        assert res.metrics["status"] == "ok" and res.metrics["ensemble_weights"]
        for d, entry in res.metrics["ensemble_weights"].items():
            t = parse_iso(d)
            allowed = [x for x in bt.eval_dates(h)
                       if x < d and parse_iso(x) + timedelta(days=h) <= t]
            assert entry["train_dates"] == len(allowed)
            if len(allowed) < DCFG["ensemble"]["min_train_dates"]:
                assert entry["status"] == "equal_no_history"


def test_ensemble_scores_ignore_later_data(conn: sqlite3.Connection) -> None:
    w = detector_world(conn)
    dates = make(conn).eval_dates(1)
    t = dates[len(dates) // 2]
    before = make(conn).rankers(1)["ensemble"](t)
    for _ in range(30):
        w.item("hackernews", at(24 * 30), backfill=True, trend=w.trend)
    assert make(conn).rankers(1)["ensemble"](t) == before


# ------------------------------------------------------------ comparison


@dataclass
class P:
    as_of: str
    trend_id: int
    score: float
    label: int


@dataclass
class R:
    detector: str
    horizon_days: int
    dates: list[str]
    points: list[P]
    metrics: dict[str, Any] = field(default_factory=dict)


def fake(name: str, good: bool, n_dates: int = 10, wf_f1: float = 0.5,
         backfill: float = 0.0) -> R:
    """Ten trends per date; trends 0-2 are positive. ``good`` ranks them first."""
    dates = [f"2026-12-{d + 1:02d}T12:00:00.000000Z" for d in range(n_dates)]
    pts = [P(d, t, float(10 - t if good else t), int(t < 3)) for d in dates for t in range(10)]
    return R(name, 1, dates, pts, {"status": "ok", "backfill_share_mean": backfill,
                                   "walk_forward": {"status": "ok", "f1": wf_f1}})


CMP = {**DCFG["comparison"], "k": 3, "min_positives": 20}


def test_sign_test() -> None:
    assert sign_test_p(5, 0) == pytest.approx(1 / 32)
    assert sign_test_p(0, 0) == 1.0
    assert sign_test_p(3, 3) > 0.5


def test_compare_verdicts() -> None:
    rows = {r.detector: r for r in compare(
        [fake("volume_only", good=False), fake("hotlist", good=True, wf_f1=0.6),
         fake("signal_sum", good=False)], CMP)}
    assert rows["volume_only"].verdict == BASELINE
    assert rows["hotlist"].verdict == BEATS
    assert rows["hotlist"].stats["wins_vs_baseline"] == 10
    assert rows["signal_sum"].verdict == NOT_BETTER


def test_compare_needs_out_of_sample_support() -> None:
    rows = {r.detector: r for r in compare(
        [fake("volume_only", good=False, wf_f1=0.7), fake("hotlist", good=True, wf_f1=0.3)],
        CMP)}
    assert rows["hotlist"].verdict == NOT_BETTER
    assert any("walk-forward" in x for x in rows["hotlist"].reasons)


@pytest.mark.parametrize(("over", "why"), [
    ({"n_dates": 3}, "evaluation dates"),
    ({"backfill": 0.9}, "backfill share"),
])
def test_compare_reports_insufficient_data(over: dict[str, Any], why: str) -> None:
    rows = compare([fake("volume_only", good=False, **over),
                    fake("ensemble", good=True, **over)], CMP)
    assert all(r.verdict == INSUFFICIENT for r in rows)
    assert all(any(why in x for x in r.reasons) for r in rows)


def test_compare_insufficient_when_few_positives() -> None:
    rows = compare([fake("volume_only", good=False), fake("hotlist", good=True)],
                   {**CMP, "min_positives": 1000})
    assert {r.verdict for r in rows} == {INSUFFICIENT}


def test_compare_insufficient_history_status_is_kept() -> None:
    empty = R("hotlist", 30, [], [], {"status": "insufficient_history", "reason": "no dates"})
    row = compare([fake("volume_only", good=False), empty], CMP)[1]
    assert row.verdict == INSUFFICIENT and "no dates" in row.reasons


# ---------------------------------------------------- readiness / CLI gate


def test_readiness_counts_live_days_only(conn: sqlite3.Connection) -> None:
    w = World(conn)
    w.prime("github")
    w.prime("hackernews")
    for day in range(7):
        w.item("github", at(24 * day))
        w.item("hackernews", at(24 * day), backfill=True)   # backfill does not count
    ready, days = readiness(conn, DCFG["comparison"])
    assert days == {"github": 7} and not ready
    for day in range(7):
        w.item("hackernews", at(24 * day + 1))
    assert readiness(conn, DCFG["comparison"])[0]


def test_compare_cli_refuses_before_enough_live_data(
        conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    World(conn).prime("github")
    monkeypatch.setattr(cli, "open_db", lambda: conn)
    assert cli.main(["compare", "--no-save"]) == 2
    assert "not running" in capsys.readouterr().out
    assert conn.execute("SELECT COUNT(*) FROM backtest_runs").fetchone()[0] == 0


# ------------------------------------------------------------- pre-registration lock


def test_shipped_detector_config_matches_the_lock() -> None:
    from liquidmexx.detectors.comparison import check_lock
    from liquidmexx.settings import PROJECT_ROOT
    assert check_lock(PROJECT_ROOT / "config") is None


def test_lock_refuses_changed_settings(tmp_path: Any) -> None:
    import shutil

    from liquidmexx.detectors.comparison import check_lock
    from liquidmexx.settings import PROJECT_ROOT
    for name in ("detectors.yaml", "detectors.lock"):
        shutil.copy(PROJECT_ROOT / "config" / name, tmp_path / name)
    with (tmp_path / "detectors.yaml").open("a") as fh:
        fh.write("\n# tweak after seeing results\n")
    assert "does not match the lock" in (check_lock(tmp_path) or "")
    (tmp_path / "detectors.lock").unlink()
    assert "missing" in (check_lock(tmp_path) or "")
