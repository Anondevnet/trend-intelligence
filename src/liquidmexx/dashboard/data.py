"""Read-only data for the dashboard. Every value comes from the database or config files.

Nothing here writes: the server opens the database read-only and with ``query_only`` on.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from liquidmexx.config import BudgetsConfig, ModelsConfig, PublishingConfig
from liquidmexx.costs.budget import BudgetGuard
from liquidmexx.detectors.comparison import check_lock, live_days
from liquidmexx.intel.movers import data_watermark, shift
from liquidmexx.publishing import kill_switch_engaged, list_drafts
from liquidmexx.scoring.engine import Scorer

FIRST_COMPARISON = "2026-11-26"


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open an existing database read-only. Never creates, migrates or writes."""
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


@dataclass
class Config:
    """Everything the dashboard reads from config/ and the environment, loaded once per page."""

    scoring: dict[str, Any]
    credibility: dict[str, float]
    detectors: dict[str, Any]
    intel: dict[str, Any]
    budgets: BudgetsConfig
    publishing: PublishingConfig
    models: ModelsConfig
    feed_categories: dict[str, str]
    config_dir: Path
    data_dir: Path
    live_posting_opt_in: bool
    live_llm_opt_in: bool


@dataclass
class SourceDays:
    source_id: str
    live_days: int
    first_day: str | None
    last_item: str | None
    days_needed: int


@dataclass
class Readiness:
    min_live_days: int
    min_ready_sources: int
    ready_sources: int
    ready: bool
    lock_problem: str | None
    sources: list[SourceDays]
    first_comparison: str = FIRST_COMPARISON


@dataclass
class TrendRow:
    slug: str
    score: float | None
    confidence: float | None
    window_mentions: float
    sources: dict[str, float]
    top_source: str | None
    top_source_share: float | None

    def flagged(self, threshold: float) -> bool:
        return self.top_source_share is not None and self.top_source_share >= threshold


@dataclass
class Spend:
    provider: str
    spent_today: float
    cap: float
    lifetime: float
    tiers: list[tuple[str, float, float]] = field(default_factory=list)


@dataclass
class Posting:
    mode: str
    kill_switch: bool
    live_opt_in: bool
    live_transport: str
    queue_counts: dict[str, int]
    open_drafts: list[dict[str, Any]]


@dataclass
class Overview:
    as_of: str
    data_watermark: str | None
    readiness: Readiness
    score_run: dict[str, Any] | None
    trends: list[TrendRow]
    concentration_warning: float
    posting: Posting
    spend: list[Spend]
    llm_live_calls: bool
    llm_live_opt_in: bool
    runs: list[dict[str, Any]]
    counts: dict[str, int]


def readiness(conn: sqlite3.Connection, cfg: Config) -> Readiness:
    cc = cfg.detectors["comparison"]
    need = int(cc["min_live_days"])
    days = live_days(conn)
    extra = {r["source_id"]: r for r in conn.execute(
        "SELECT source_id, MIN(substr(available_at,1,10)) first_day, MAX(available_at) last_item"
        " FROM v_raw_items WHERE is_backfill=0 AND is_initial_snapshot=0 GROUP BY source_id")}
    known = sorted(set(days) | {r["source_id"] for r in conn.execute(
        "SELECT DISTINCT source_id FROM collection_runs")})
    rows = []
    for src in known:
        n = days.get(src, 0)
        e = extra.get(src)
        rows.append(SourceDays(src, n, e["first_day"] if e else None,
                               e["last_item"] if e else None, max(need - n, 0)))
    ready_n = sum(1 for r in rows if r.live_days >= need)
    return Readiness(need, int(cc["min_ready_sources"]), ready_n,
                     ready_n >= int(cc["min_ready_sources"]), check_lock(cfg.config_dir), rows)


def window_sources(conn: sqlite3.Connection, cfg: Config, as_of: str
                   ) -> dict[int, dict[str, float]]:
    """Weighted eligible mentions per trend and source in the intel window ending at as_of."""
    start = shift(as_of, float(cfg.intel["window_hours"]))
    mentions, _ = Scorer(conn, cfg.scoring, cfg.credibility).eligible_mentions(as_of)
    out: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for m in mentions:
        if m.available_at > start and m.weight > 0:
            out[m.trend_id][m.source_id] += m.weight
    return out


def trend_rows(conn: sqlite3.Connection, cfg: Config, as_of: str, limit: int = 15
               ) -> tuple[dict[str, Any] | None, list[TrendRow]]:
    """Top trends of the latest scoring run known at as_of, plus the busiest by mentions."""
    shares = window_sources(conn, cfg, as_of)
    run = conn.execute("SELECT id, as_of, scorer_version FROM scoring_runs WHERE as_of<=?"
                       " ORDER BY as_of DESC, id DESC LIMIT 1", (as_of,)).fetchone()
    scored: dict[int, tuple[float, float]] = {}
    if run is not None:
        scored = {int(r["trend_id"]): (float(r["score"]), float(r["confidence"]))
                  for r in conn.execute("SELECT trend_id, score, confidence FROM trend_scores"
                                        " WHERE run_id=?", (run["id"],))}
    top_scored = sorted(scored, key=lambda t: -scored[t][0])[:limit]
    busiest = sorted(shares, key=lambda t: -sum(shares[t].values()))[:limit]
    ids = list(dict.fromkeys(top_scored + busiest))
    slugs = {int(r["id"]): r["slug"] for r in conn.execute(
        f"SELECT id, slug FROM trends WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids \
        else {}
    rows = []
    for tid in ids:
        src = {k: round(v, 4) for k, v in sorted(shares.get(tid, {}).items())}
        total = sum(src.values())
        top = max(src, key=lambda s: (src[s], s)) if src else None
        sc = scored.get(tid)
        rows.append(TrendRow(slugs[tid], sc[0] if sc else None, sc[1] if sc else None,
                             round(total, 4), src, top,
                             round(src[top] / total, 4) if top else None))
    rows.sort(key=lambda r: (r.score is None, -(r.score or 0.0), -r.window_mentions, r.slug))
    info = dict(run) if run is not None else None
    return info, rows


def posting(conn: sqlite3.Connection, cfg: Config) -> Posting:
    counts: dict[str, int] = {}
    open_rows = []
    for draft, status in list_drafts(conn, include_closed=True):
        counts[status] = counts.get(status, 0) + 1
        if status in ("pending_review", "approved", "blocked_by_gate", "publishing"):
            open_rows.append({"id": draft.id, "status": status, "created_at": draft.created_at,
                              "claim_status": draft.claim_status, "text": draft.text,
                              "sources": len(draft.sources)})
    return Posting(cfg.publishing.mode, kill_switch_engaged(cfg.publishing, cfg.data_dir),
                   cfg.live_posting_opt_in, "none installed (dry-run only)", counts, open_rows)


def spend(conn: sqlite3.Connection, cfg: Config, as_of: str) -> list[Spend]:
    guard = BudgetGuard(conn, cfg.budgets)
    out = []
    for provider in ("anthropic", "x"):
        budget = cfg.budgets.providers.get(provider)
        if budget is None:
            continue
        s = guard.status(provider)
        tiers = [(t, guard.spent_today(provider, as_of, tier=t), cap)
                 for t, cap in sorted(budget.tiers.items())]
        out.append(Spend(provider, guard.spent_today(provider, as_of), budget.daily_cap_usd,
                         s.lifetime_spent_usd, tiers))
    return out


def overview(conn: sqlite3.Connection, cfg: Config, as_of: str) -> Overview:
    run, trends = trend_rows(conn, cfg, as_of)
    runs = [dict(r) for r in conn.execute(
        "SELECT source_id, mode, status, started_at, items_new, observations_written, error"
        " FROM collection_runs WHERE id IN (SELECT MAX(id) FROM collection_runs"
        " GROUP BY source_id, mode) ORDER BY source_id, mode")]
    counts = {t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
              for t in ("raw_items", "observations", "trends", "trend_mentions", "api_usage")}
    return Overview(as_of, data_watermark(conn, as_of), readiness(conn, cfg), run, trends,
                    float(cfg.intel["concentration_warning"]), posting(conn, cfg),
                    spend(conn, cfg, as_of), cfg.models.live_calls, cfg.live_llm_opt_in, runs,
                    counts)

