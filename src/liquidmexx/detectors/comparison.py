"""Stage 6 detector comparison: honest verdicts, INSUFFICIENT DATA when evidence is thin.

No detector (the ensemble included) is ranked above the baseline by default. It has to beat
``volume_only`` on per-date top-k hits by a one-sided paired sign test, and must not do
worse out of sample (walk-forward F1).
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

INSUFFICIENT = "INSUFFICIENT DATA"
BASELINE = "BASELINE"
BEATS = "BEATS BASELINE"
NOT_BETTER = "NOT BETTER THAN BASELINE"


class _Point(Protocol):
    @property
    def as_of(self) -> str: ...
    @property
    def trend_id(self) -> int: ...
    @property
    def score(self) -> float: ...
    @property
    def label(self) -> int: ...


class _Result(Protocol):
    @property
    def detector(self) -> str: ...
    @property
    def horizon_days(self) -> int: ...
    @property
    def dates(self) -> list[str]: ...
    @property
    def points(self) -> Sequence[_Point]: ...
    @property
    def metrics(self) -> dict[str, Any]: ...


@dataclass
class Row:
    detector: str
    horizon_days: int
    verdict: str
    reasons: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)


def sign_test_p(wins: int, losses: int) -> float:
    """One-sided exact binomial p-value of >= ``wins`` successes in wins+losses fair trials."""
    n = wins + losses
    if n == 0:
        return 1.0
    return float(sum(math.comb(n, i) for i in range(wins, n + 1)) / 2 ** n)


def per_date_hits(res: _Result, k: int) -> dict[str, int]:
    by_date: dict[str, list[_Point]] = defaultdict(list)
    for p in res.points:
        by_date[p.as_of].append(p)
    out: dict[str, int] = {}
    for d in res.dates:
        pts = sorted(by_date.get(d, []), key=lambda p: (-p.score, p.trend_id))
        out[d] = sum(p.label for p in pts[:k])
    return out


def _stats(res: _Result, k: int) -> dict[str, Any]:
    hits = per_date_hits(res, k)
    positives = sum(p.label for p in res.points)
    negatives = len(res.points) - positives
    picks = 0
    by_date: dict[str, int] = defaultdict(int)
    for p in res.points:
        by_date[p.as_of] += 1
    for d in res.dates:
        picks += min(k, by_date.get(d, 0))
    tp = sum(hits.values())
    wf = res.metrics.get("walk_forward", {}) if res.metrics else {}
    return {
        "n_dates": len(res.dates),
        "positives": positives,
        f"hits_at_{k}": f"{tp}/{picks}",
        f"recall_at_{k}": round(tp / positives, 6) if positives else None,
        f"false_positive_rate_at_{k}": round((picks - tp) / negatives, 6) if negatives else None,
        "walk_forward_f1": wf.get("f1") if wf.get("status") == "ok" else None,
        "backfill_share_mean": res.metrics.get("backfill_share_mean") if res.metrics else None,
    }


def compare(results: Sequence[_Result], cfg: Mapping[str, Any]) -> list[Row]:
    """Rows for one horizon. ``cfg`` is the ``comparison`` block of detectors.yaml."""
    k = int(cfg["k"])
    base_name = str(cfg["baseline"])
    by_name = {r.detector: r for r in results}
    base = by_name.get(base_name)
    base_hits = per_date_hits(base, k) if base else {}
    base_f1 = None
    if base is not None and base.metrics.get("walk_forward", {}).get("status") == "ok":
        base_f1 = base.metrics["walk_forward"]["f1"]
    rows: list[Row] = []
    for res in results:
        row = Row(res.detector, res.horizon_days, "", stats=_stats(res, k))
        st = row.stats
        if res.metrics.get("status") != "ok":
            row.reasons.append(str(res.metrics.get("reason", res.metrics.get("status"))))
        if st["n_dates"] < int(cfg["min_eval_dates"]):
            row.reasons.append(f"{st['n_dates']} evaluation dates < {cfg['min_eval_dates']}")
        if st["positives"] < int(cfg["min_positives"]):
            row.reasons.append(f"{st['positives']} positive labels < {cfg['min_positives']}")
        share = st["backfill_share_mean"]
        if share is not None and share > float(cfg["max_backfill_share"]):
            row.reasons.append(f"backfill share {share:.2f} > {cfg['max_backfill_share']}"
                               " (hindsight-biased evidence)")
        if base is None:
            row.reasons.append(f"baseline {base_name} was not run")
        if row.reasons:
            row.verdict = INSUFFICIENT
        elif res.detector == base_name:
            row.verdict = BASELINE
        else:
            mine = per_date_hits(res, k)
            wins = sum(1 for d in res.dates if mine.get(d, 0) > base_hits.get(d, 0))
            losses = sum(1 for d in res.dates if mine.get(d, 0) < base_hits.get(d, 0))
            p = sign_test_p(wins, losses)
            st.update({"wins_vs_baseline": wins, "losses_vs_baseline": losses,
                       "sign_test_p": round(p, 6)})
            f1 = st["walk_forward_f1"]
            oos_ok = f1 is not None and (base_f1 is None or f1 >= base_f1)
            if wins > losses and p <= float(cfg["sign_test_alpha"]) and oos_ok:
                row.verdict = BEATS
            else:
                row.verdict = NOT_BETTER
                if not oos_ok:
                    row.reasons.append("walk-forward F1 below baseline or unavailable")
                if p > float(cfg["sign_test_alpha"]):
                    row.reasons.append(f"sign test p={p:.3f} > {cfg['sign_test_alpha']}")
        rows.append(row)
    return rows


def live_days(conn: sqlite3.Connection) -> dict[str, int]:
    """Distinct UTC days with live (non-backfill, non-baseline) items, per source."""
    return {r["source_id"]: int(r["days"]) for r in conn.execute(
        "SELECT source_id, COUNT(DISTINCT substr(available_at,1,10)) days FROM v_raw_items"
        " WHERE is_backfill=0 AND is_initial_snapshot=0 GROUP BY source_id")}


def readiness(conn: sqlite3.Connection, cfg: Mapping[str, Any]) -> tuple[bool, dict[str, int]]:
    days = live_days(conn)
    ready = sum(1 for d in days.values() if d >= int(cfg["min_live_days"]))
    return ready >= int(cfg["min_ready_sources"]), days


def check_lock(config_dir: Path) -> str | None:
    """None if detectors.yaml matches the committed lock, else why the comparison must not run."""
    lock = config_dir / "detectors.lock"
    if not lock.exists():
        return "config/detectors.lock is missing"
    expected = None
    for line in lock.read_text(encoding="utf-8").splitlines():
        if line.startswith("sha256:"):
            expected = line.split(":", 1)[1].strip()
    if not expected:
        return "config/detectors.lock has no sha256 line"
    actual = hashlib.sha256((config_dir / "detectors.yaml").read_bytes()).hexdigest()
    if actual != expected:
        return (f"config/detectors.yaml (sha256 {actual[:12]}) does not match the lock"
                f" ({expected[:12]}); detector settings may not change after results are seen")
    return None
