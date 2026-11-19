"""Honest historical backfill.

Only data whose historical availability is genuinely knowable is backfilled:

* GitHub star history from ``starred_at`` timestamps (stargazers API). Caveat:
  users who starred and later un-starred are invisible, so historical counts
  are reconstructed from *current* stargazers only.
* Hacker News stories from their public ``created_at`` time. Their *points*
  history is NOT knowable, so no historical points are written.

Everything written here carries ``is_backfill = 1``.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta

from liquidmexx.collector import persist
from liquidmexx.config import load_budgets, load_sources
from liquidmexx.costs.budget import BudgetGuard
from liquidmexx.settings import Settings, get_settings
from liquidmexx.sources.base import CollectContext, CollectResult
from liquidmexx.sources.github import GitHubAdapter
from liquidmexx.sources.hackernews import HackerNewsAdapter
from liquidmexx.sources.http import PoliteClient, SourceHTTPError
from liquidmexx.store import Observation, RunStats, Store
from liquidmexx.timeutil import now_iso, parse_iso, to_iso

log = logging.getLogger(__name__)


def daily_cumulative(starred_at: list[str], until: datetime) -> list[tuple[str, int]]:
    """End-of-day cumulative star counts for each UTC day up to (not incl.) ``until``'s day."""
    if not starred_at:
        return []
    per_day = Counter(parse_iso(s).date() for s in starred_at)
    day = min(per_day)
    last = until.astimezone(UTC).date() - timedelta(days=1)
    total = 0
    out: list[tuple[str, int]] = []
    while day <= last:
        total += per_day.get(day, 0)
        end = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=UTC)
        out.append((to_iso(end), total))
        day += timedelta(days=1)
    return out


def backfill_github_stars(
    conn: sqlite3.Connection, *, max_repos: int = 20, max_stars: int = 2000,
    settings: Settings | None = None,
) -> RunStats:
    settings = settings or get_settings()
    store = Store(conn)
    store.ensure_source("github", "code")
    guard = BudgetGuard(conn, load_budgets())
    adapter = GitHubAdapter()
    run_id = store.start_run("github", mode="backfill")
    stats = RunStats()
    headers = adapter.client_headers(settings)
    headers["Accept"] = "application/vnd.github.star+json"
    candidates = conn.execute(
        "SELECT r.external_id, MAX(o.value) AS stars FROM raw_items r"
        " JOIN observations o ON o.subject_type='repo' AND o.subject_key=r.external_id"
        "  AND o.metric='stars' AND o.is_backfill=0"
        " WHERE r.source_id='github' AND r.item_type='repo'"
        "  AND NOT EXISTS (SELECT 1 FROM observations b WHERE b.subject_type='repo'"
        "   AND b.subject_key=r.external_id AND b.metric='stars' AND b.is_backfill=1)"
        " GROUP BY r.external_id HAVING stars <= ? ORDER BY stars DESC LIMIT ?",
        (max_stars, max_repos),
    ).fetchall()
    now = datetime.now(UTC)
    status = "ok"
    with PoliteClient("github", guard, min_interval_s=0.8 if settings.github_token else 60.0,
                      run_id=run_id, headers=headers) as client:
        for row in candidates:
            key = row["external_id"]
            stamps: list[str] = []
            try:
                for page in range(1, 401):
                    data = client.get_json(f"https://api.github.com/repos/{key}/stargazers",
                                           params={"per_page": 100, "page": page})
                    stamps += [d["starred_at"] for d in data if d.get("starred_at")]
                    if len(data) < 100:
                        break
            except SourceHTTPError as exc:
                stats.errors.append(f"{key}: {exc}")
                status = "partial"
                if exc.status_code in (403, 429):
                    break
                continue
            obs = [Observation("repo", key, "stars", float(n), observed_at=t, available_at=t)
                   for t, n in daily_cumulative(stamps, now)]
            result = CollectResult(observations=obs)
            persist(store, "github", result, run_id, now_iso(), is_backfill=True, stats=stats)
        stats.requests_made = client.requests_made
    store.finish_run(run_id, status, stats)
    return stats


def backfill_hackernews(
    conn: sqlite3.Connection, *, days: int = 30, min_points: int = 3,
    settings: Settings | None = None,
) -> RunStats:
    settings = settings or get_settings()
    store = Store(conn)
    store.ensure_source("hackernews", "community")
    guard = BudgetGuard(conn, load_budgets())
    run_id = store.start_run("hackernews", mode="backfill")
    stats = RunStats()
    now = datetime.now(UTC)
    status = "ok"
    params = load_sources().get("hackernews")
    with PoliteClient("hackernews", guard, min_interval_s=1.0, run_id=run_id) as client:
        ctx = CollectContext(client=client, store=store, settings=settings,
                             params=params.params if params else {}, fetched_at=now_iso(),
                             last_success_at=None)
        for d in range(days, 0, -1):
            start = int((now - timedelta(days=d)).timestamp())
            end = int((now - timedelta(days=d - 1)).timestamp())
            try:
                result = HackerNewsAdapter.backfill_window(ctx, start, end, min_points)
            except SourceHTTPError as exc:
                stats.errors.append(f"day -{d}: {exc}")
                status = "partial"
                continue
            persist(store, "hackernews", result, run_id, now_iso(), is_backfill=True, stats=stats)
        stats.requests_made = client.requests_made
    store.finish_run(run_id, status, stats)
    return stats
