"""Read-only data for the dashboard. Every value comes from the database or config files.

Nothing here writes: the server opens the database read-only and with ``query_only`` on.
"""

from __future__ import annotations

import json
import sqlite3
from bisect import bisect_right
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
from liquidmexx.timeutil import parse_iso
from liquidmexx.trends.identity import UNLINKED_SOURCES

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
class Kpi:
    """One top-level tile. ``tone`` is ok / warn / na (na = not collected or not validated)."""

    label: str
    value: str
    sub: str
    tone: str = "ok"
    help: str = ""


@dataclass
class Velocity:
    slug: str
    n24: int
    n1: int
    g1h: float | None
    g6h: float | None
    g24h: float | None
    sources: int
    first_seen: str | None
    stage: str
    name: str = ""


@dataclass
class Command:
    kpis: list[Kpi]
    velocities: list[Velocity]
    hourly: list[int]  # mentions per hour, oldest first, 24 entries ending at as_of
    items_24h: int
    social: Social | None = None


@dataclass
class Overview:
    command: Command
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
    suggested: list[Any] = field(default_factory=list)  # composer.Composed, not saved


def readiness(conn: sqlite3.Connection, cfg: Config) -> Readiness:
    cc = cfg.detectors["comparison"]
    need = int(cc["min_live_days"])
    days = live_days(conn)
    extra = {r["source_id"]: r for r in conn.execute(
        "SELECT source_id, MIN(substr(available_at,1,10)) first_day, MAX(available_at) last_item"
        " FROM v_raw_items WHERE is_backfill=0 AND is_initial_snapshot=0 GROUP BY source_id")}
    known = sorted((set(days) | {r["source_id"] for r in conn.execute(
        "SELECT DISTINCT source_id FROM collection_runs")}) - UNLINKED_SOURCES)
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
    cmd = command(conn, cfg, as_of)
    from liquidmexx.publishing.composer import compose
    suggested = compose(conn, cfg.publishing, cmd.velocities, as_of, limit=2)
    return Overview(cmd, as_of, data_watermark(conn, as_of),
                    readiness(conn, cfg), run, trends,
                    float(cfg.intel["concentration_warning"]), posting(conn, cfg),
                    spend(conn, cfg, as_of), cfg.models.live_calls, cfg.live_llm_opt_in, runs,
                    counts, suggested)



# ---- command center: velocity and lifecycle, all from stored mentions -------------------------
MIN_MENTIONS = 3  # below this a trend is only "detection"
SPIKE_MIN_1H = 5
SPIKE_FACTOR = 3.0


def growth(now: int, before: int) -> float | None:
    """Fractional growth; None when there is nothing before to compare with (new activity)."""
    return None if before <= 0 else (now - before) / before


def lifecycle(n24: int, p24: int, n6: int, p6: int) -> str:
    """Rule-based, descriptive stage. Not validated as a predictor (see the Stage 5 backtest)."""
    if n24 < MIN_MENTIONS:
        return "Detection"
    g24, g6 = growth(n24, p24), growth(n6, p6)
    if g6 is not None and g6 <= -0.3:
        return "Cooling"
    if (g6 is None or g6 >= 1.0) and (g24 is None or g24 >= 1.0):
        return "Accelerating"
    if (g24 is None or g24 > 0.0) and (g6 is None or g6 > 0.0):
        return "Emerging"
    return "Steady"


# ---- social tiles: Mastodon (free) stands in while X is off ----------------------------------
SOCIAL_SOURCE = "mastodon"
ENGAGEMENT = ("likes", "reposts", "replies")
INFLUENCE_MIN = 10        # engagement on an account's posts (24h) to count as influential
ACCEL_MIN_POSTS = 5       # posts seen 2h ago needed before an acceleration figure is shown


@dataclass
class Social:
    posts_24h: int
    engagement_24h: int
    accel: float | None          # growth of engagement gained: last hour vs the hour before
    accel_posts: int
    influential: int
    top_account: str | None
    top_engagement: int
    hourly_posts: list[int] = field(default_factory=lambda: [0] * 24)    # posted per hour
    hourly_engagement: list[int] = field(default_factory=lambda: [0] * 24)  # reactions on those
    top_posts: list[dict[str, Any]] = field(default_factory=list)
    top_accounts: list[tuple[str, int]] = field(default_factory=list)


def social(conn: sqlite3.Connection, as_of: str) -> Social | None:
    """Engagement signals from stored Mastodon posts. None when no such posts are stored."""
    t24, t2, t1 = shift(as_of, 24), shift(as_of, 2), shift(as_of, 1)
    posts = conn.execute(
        "SELECT external_id, author, title, url, published_at FROM raw_items"
        " WHERE source_id=? AND published_at>? AND available_at<=?",
        (SOCIAL_SOURCE, t24, as_of)).fetchall()
    if not posts:
        return None
    author = {f"{SOCIAL_SOURCE}:{r['external_id']}": r["author"] for r in posts}
    by_key = {f"{SOCIAL_SOURCE}:{r['external_id']}": r for r in posts}
    series: dict[str, dict[str, list[tuple[str, float]]]] = {}
    marks = ",".join("?" * len(ENGAGEMENT))
    for r in conn.execute(
            "SELECT subject_key, metric, value, available_at FROM observations WHERE source_id=?"
            f" AND metric IN ({marks}) AND available_at<=? AND available_at>=?"
            " ORDER BY available_at",
            (SOCIAL_SOURCE, *ENGAGEMENT, as_of, shift(t24, 3))):
        if r["subject_key"] in author:
            series.setdefault(r["subject_key"], {}).setdefault(r["metric"], []).append(
                (r["available_at"], float(r["value"])))

    def at_time(key: str, when: str) -> float | None:
        total, seen = 0.0, False
        for pts in series.get(key, {}).values():
            i = bisect_right([t for t, _ in pts], when)
            if i:
                total += pts[i - 1][1]
                seen = True
        return total if seen else None

    per_author: dict[str, float] = defaultdict(float)
    total_now = 0.0
    hourly_posts, hourly_eng = [0] * 24, [0] * 24
    end = parse_iso(as_of)
    ranked: list[dict[str, Any]] = []
    gain_recent = gain_prev = 0.0
    counted = 0
    for key, who in author.items():
        now = at_time(key, as_of) or 0.0
        total_now += now
        p = by_key[key]
        age_h = int((end - parse_iso(p["published_at"])).total_seconds() // 3600)
        if 0 <= age_h < 24:
            hourly_posts[23 - age_h] += 1
            hourly_eng[23 - age_h] += int(now)
        ranked.append({"engagement": int(now), "author": who, "url": p["url"],
                       "text": (p["title"] or "").strip()})
        if who:
            per_author[who] += now
        e1, e2 = at_time(key, t1), at_time(key, t2)
        if e2 is not None and e1 is not None:  # seen before the window opened: comparable
            counted += 1
            gain_recent += now - e1
            gain_prev += e1 - e2
    accel = growth(int(gain_recent), int(gain_prev)) if counted >= ACCEL_MIN_POSTS else None
    big = {a: v for a, v in per_author.items() if v >= INFLUENCE_MIN}
    top = max(big, key=lambda a: (big[a], a)) if big else None
    ranked.sort(key=lambda r: (-r["engagement"], r["url"] or ""))
    accounts = sorted(((a, int(v)) for a, v in per_author.items() if v > 0),
                      key=lambda x: (-x[1], x[0]))[:5]
    return Social(len(author), int(total_now), accel, counted, len(big), top,
                  int(big[top]) if top else 0, hourly_posts, hourly_eng,
                  [r for r in ranked if r["engagement"] > 0][:5], accounts)


def command(conn: sqlite3.Connection, cfg: Config, as_of: str) -> Command:
    b = {h: shift(as_of, h) for h in (1, 2, 6, 12, 24, 48)}
    rows = conn.execute(
        "SELECT DISTINCT m.trend_id, m.raw_item_id, t.slug, t.name, m.source_id, m.available_at"
        " FROM trend_mentions m JOIN trends t ON t.id=m.trend_id"
        " JOIN v_raw_items r ON r.id=m.raw_item_id"
        " WHERE m.available_at<=? AND m.available_at>? AND r.is_backfill=0"
        " AND r.is_initial_snapshot=0", (as_of, b[48])).fetchall()
    per: dict[str, dict[str, Any]] = {}
    hourly = [0] * 24
    end = parse_iso(as_of)
    for r in rows:
        d = per.setdefault(r["slug"], {"n1": 0, "p1": 0, "n6": 0, "p6": 0, "n24": 0, "p24": 0,
                                       "src": set(), "first": r["available_at"],
                                       "name": r["name"]})
        av = r["available_at"]
        d["first"] = min(d["first"], av)
        if av > b[24]:
            d["n24"] += 1
            d["src"].add(r["source_id"])
            age_h = int((end - parse_iso(av)).total_seconds() // 3600)
            if 0 <= age_h < 24:
                hourly[23 - age_h] += 1
        else:
            d["p24"] += 1
        if av > b[1]:
            d["n1"] += 1
        elif av > b[2]:
            d["p1"] += 1
        if av > b[6]:
            d["n6"] += 1
        elif av > b[12]:
            d["p6"] += 1
    vel = []
    spikes = emerging = 0
    for slug, d in per.items():
        if d["n24"] == 0:
            continue
        stage = lifecycle(d["n24"], d["p24"], d["n6"], d["p6"])
        rest_mean = (d["n24"] - d["n1"]) / 23
        if d["n1"] >= SPIKE_MIN_1H and d["n1"] >= SPIKE_FACTOR * rest_mean:
            spikes += 1
        if stage in ("Emerging", "Accelerating"):
            emerging += 1
        vel.append(Velocity(slug, d["n24"], d["n1"], growth(d["n1"], d["p1"]),
                            growth(d["n6"], d["p6"]), growth(d["n24"], d["p24"]),
                            len(d["src"]), d["first"], stage, d["name"]))
    vel.sort(key=lambda v: (-v.n24, v.slug))
    items = conn.execute(
        "SELECT COUNT(*) c, COUNT(DISTINCT source_id) s FROM v_raw_items WHERE available_at<=?"
        " AND available_at>? AND is_backfill=0 AND is_initial_snapshot=0", (as_of, b[24])
    ).fetchone()
    n1_all = sum(d["n1"] for d in per.values())
    p1_all = sum(d["p1"] for d in per.values())
    g = growth(n1_all, p1_all)
    flagged = sum(1 for t in trend_rows(conn, cfg, as_of)[1]
                  if t.flagged(float(cfg.intel["concentration_warning"])))
    soc = social(conn, as_of)
    rising = sum(1 for v in vel if v.stage == "Accelerating")
    cooling = sum(1 for v in vel if v.stage == "Cooling")
    h_accel = ("Are people reacting faster or slower than an hour ago? Counts likes, reposts and"
               " replies on public Mastodon posts (X is off).")
    h_infl = ("Accounts whose recent posts earned real reactions. Mastodon only for now; X data"
              " is not collected.")
    if soc is None:
        accel_kpi = Kpi("Engagement acceleration", "n/a",
                        "not collected: needs social posts with engagement (X is off)", "na",
                        h_accel)
        infl_kpi = Kpi("Influential accounts", "n/a", "not collected: needs social account data",
                       "na", h_infl)
    else:
        if soc.accel is None:
            accel_kpi = Kpi("Engagement acceleration", "collecting",
                            f"needs 2 hours of history on {ACCEL_MIN_POSTS}+ posts"
                            f" ({soc.accel_posts} so far)", "na", h_accel)
        else:
            trend_word = "speeding up" if soc.accel > 0.05 else (
                "slowing down" if soc.accel < -0.05 else "about the same")
            accel_kpi = Kpi("Engagement acceleration", f"{soc.accel:+.0%}",
                            f"Reactions are {trend_word}: this hour vs the hour before"
                            f" ({soc.accel_posts} Mastodon posts)", "ok", h_accel)
        infl_kpi = Kpi("Influential accounts", str(soc.influential),
                       f"accounts whose posts earned {INFLUENCE_MIN}+ reactions in 24h"
                       + (f". Top: {soc.top_account} ({soc.top_engagement})"
                          if soc.top_account else ""), "ok", h_infl)
    kpis = [
        Kpi("Items monitored (24h)", f"{items['c']:,}",
            f"posts, repos, articles and listings from {items['s']} sources. Social posts come"
            " from Mastodon only (X is off)", "ok",
            "Everything the collectors picked up in the last 24 hours."),
        Kpi("Emerging trends", str(emerging), "topics growing over both the last 24h and 6h",
            "ok", "Topics whose mentions are going up. Simple rules, not a forecast."),
        Kpi("Trend velocity", "new" if g is None else f"{g:+.0%}",
            f"mentions this hour: {n1_all} (vs {p1_all} the hour before)", "ok",
            "How fast overall mentions are changing compared with the hour before."),
        accel_kpi,
        infl_kpi,
        Kpi("Viral probability", "n/a",
            "no validated predictor yet: our score has not beaten plain mention volume", "na",
            "We will not show a made-up number. This appears only once a method proves itself."),
        Kpi("Anomaly alerts", str(spikes),
            f"sudden spikes: a topic's last hour is {SPIKE_FACTOR:g}x its usual (min"
            f" {SPIKE_MIN_1H}). {flagged} topics get most of their buzz from one source",
            "warn" if spikes or flagged else "ok",
            "Spikes are unusual jumps. Single-source flags mean the buzz may not be broad."),
        Kpi("Narrative shifts", f"{rising}\u2191 {cooling}\u2193",
            "topics surging vs fading (simple rules)", "ok",
            "Up arrow: topics surging. Down arrow: topics fading. Descriptive only."),
    ]
    return Command(kpis, vel[:15], hourly, int(items["c"]), soc)


# ---- post creator: suggested and queued drafts (read-only) ------------------------------------
@dataclass
class DraftsView:
    suggested: list[Any]            # publishing.composer.Composed, not saved anywhere
    queued: list[dict[str, Any]]    # newest first
    mode: str


def drafts_view(conn: sqlite3.Connection, cfg: Config, as_of: str) -> DraftsView:
    from liquidmexx.publishing import status_of
    from liquidmexx.publishing.composer import basis_of, compose

    suggested = compose(conn, cfg.publishing, command(conn, cfg, as_of).velocities, as_of,
                        limit=5)
    queued = []
    for r in conn.execute("SELECT id, created_at, created_by, text, claim_status, sources_json,"
                          " meta_json FROM post_drafts ORDER BY id DESC LIMIT 20"):
        queued.append({"id": r["id"], "created_at": r["created_at"], "by": r["created_by"],
                       "text": r["text"], "claim": r["claim_status"],
                       "sources": len(json.loads(r["sources_json"])),
                       "status": status_of(conn, int(r["id"])), "basis": basis_of(r["meta_json"])})
    return DraftsView(suggested, queued, cfg.publishing.mode)
