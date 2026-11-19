"""Runs source adapters and persists their output atomically per run."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import httpx

from liquidmexx.config import SourceConfig, load_budgets, load_sources
from liquidmexx.costs.budget import BudgetGuard
from liquidmexx.redaction import redact
from liquidmexx.settings import Settings, get_settings
from liquidmexx.sources import ADAPTERS
from liquidmexx.sources.base import CollectContext, CollectResult, SourceAdapter
from liquidmexx.sources.http import PoliteClient
from liquidmexx.store import RunStats, Store
from liquidmexx.timeutil import now_iso, parse_iso

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    source_id: str
    status: str
    stats: RunStats
    reason: str = ""


def is_due(store: Store, source_id: str, cfg: SourceConfig, now: str) -> bool:
    last = store.last_successful_run(source_id)
    if last is None:
        return True
    # 1-minute slack so a 15-minute scheduler doesn't drift past 60-minute intervals.
    return parse_iso(now) - parse_iso(last) >= timedelta(minutes=cfg.interval_minutes - 1)


def persist(
    store: Store, source_id: str, result: CollectResult, run_id: int, fetched_at: str,
    *, is_backfill: bool = False, stats: RunStats,
) -> None:
    conn = store.conn
    conn.execute("BEGIN IMMEDIATE")
    try:
        for item in result.items:
            available = None
            if is_backfill:
                if not item.published_at:
                    continue  # cannot date it honestly -> excluded from backfill
                available = item.published_at
            _, new = store.insert_raw_item(source_id, item, run_id, fetched_at,
                                           available_at=available, is_backfill=is_backfill)
            stats.items_seen += 1
            stats.items_new += int(new)
        stats.observations_written += store.add_observations(
            source_id, result.observations, run_id, fetched_at, is_backfill=is_backfill
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def run_source(
    conn: sqlite3.Connection,
    source_id: str,
    cfg: SourceConfig,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] | None = None,
) -> RunReport:
    settings = settings or get_settings()
    store = Store(conn)
    adapter_cls = ADAPTERS.get(source_id)
    if adapter_cls is None:
        return RunReport(source_id, "skipped", RunStats(), "unknown source")
    adapter: SourceAdapter = adapter_cls()
    usable, reason = adapter.availability(settings)
    if not usable:
        return RunReport(source_id, "skipped", RunStats(), reason)

    store.ensure_source(source_id, adapter.kind)
    guard = BudgetGuard(conn, load_budgets())
    last_ok = store.last_successful_run(source_id)
    run_id = store.start_run(source_id)
    stats = RunStats()
    fetched_at = now_iso()
    client = PoliteClient(
        source_id, guard, min_interval_s=adapter.pacing(settings), run_id=run_id,
        headers=adapter.client_headers(settings), transport=transport,
        sleep=sleep if sleep is not None else time.sleep,
    )
    secrets = settings.secret_values()
    try:
        ctx = CollectContext(client=client, store=store, settings=settings, params=cfg.params,
                             fetched_at=fetched_at, last_success_at=last_ok)
        result = adapter.collect(ctx)
        stats.requests_made = client.requests_made
        stats.errors = [redact(e, secrets) for e in result.errors]
        persist(store, source_id, result, run_id, fetched_at, stats=stats)
        status = "partial" if result.errors else "ok"
    except Exception as exc:  # recorded, never crashes the scheduler
        stats.requests_made = client.requests_made
        stats.errors.append(redact(f"{type(exc).__name__}: {exc}", secrets))
        status = "error"
        log.error("%s failed: %s", source_id, stats.errors[-1])
    finally:
        client.close()
    store.finish_run(run_id, status, stats)
    log.info("%s: %s items_new=%d obs=%d requests=%d", source_id, status, stats.items_new,
             stats.observations_written, stats.requests_made)
    return RunReport(source_id, status, stats)


def run_collection(
    conn: sqlite3.Connection, *, only: list[str] | None = None, due_only: bool = False,
    settings: Settings | None = None,
) -> list[RunReport]:
    configs = load_sources()
    store = Store(conn)
    now = now_iso()
    reports: list[RunReport] = []
    for source_id, cfg in configs.items():
        if only and source_id not in only:
            continue
        if not only and not cfg.enabled:
            continue
        if due_only and not is_due(store, source_id, cfg, now):
            continue
        reports.append(run_source(conn, source_id, cfg, settings=settings))
    return reports
