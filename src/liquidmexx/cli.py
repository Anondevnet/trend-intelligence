"""Command-line entry point: ``uv run liquidmexx <command>``."""

from __future__ import annotations

import argparse
import fcntl
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from liquidmexx.config import load_budgets, load_sources, load_taxonomy
from liquidmexx.costs.budget import BudgetGuard
from liquidmexx.db import backup, open_db
from liquidmexx.redaction import setup_logging
from liquidmexx.settings import get_settings
from liquidmexx.sources import ADAPTERS
from liquidmexx.trends.identity import link_new_items, seed_taxonomy

log = logging.getLogger("liquidmexx")


@contextmanager
def single_instance(name: str) -> Iterator[bool]:
    """Non-blocking file lock so overlapping launchd runs don't collide."""
    lock_dir = get_settings().data_dir
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / f".{name}.lock").open("w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def cmd_migrate(_: argparse.Namespace) -> int:
    conn = open_db()
    added = seed_taxonomy(conn, load_taxonomy())
    print(f"database ready at {get_settings().db_path} (taxonomy aliases added: {added})")
    return 0


def cmd_sources(_: argparse.Namespace) -> int:
    settings = get_settings()
    configs = load_sources()
    conn = open_db()
    print(f"{'source':<14}{'enabled':<9}{'every':<8}{'usable':<8}{'last ok run (UTC)':<30}note")
    for sid, cfg in configs.items():
        adapter_cls = ADAPTERS.get(sid)
        usable, note = adapter_cls().availability(settings) if adapter_cls else (False, "unknown")
        last = conn.execute(
            "SELECT MAX(started_at) t FROM collection_runs WHERE source_id=? AND mode='live'"
            " AND status IN ('ok','partial')", (sid,)).fetchone()["t"]
        every = f"{cfg.interval_minutes}m"
        print(f"{sid:<14}{cfg.enabled!s:<9}{every:<8}{usable!s:<8}"
              f"{(last or '-'):<30}{note}")
    print("\ncredentials present:", ", ".join(
        k for k, v in settings.credential_status().items() if v) or "none")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    from liquidmexx.collector import run_collection

    with single_instance("collect") as acquired:
        if not acquired:
            log.warning("another collection is running; exiting")
            return 0
        conn = open_db()
        seed_taxonomy(conn, load_taxonomy())
        reports = run_collection(conn, only=args.source or None, due_only=args.due)
        linked = link_new_items(conn)
    for r in reports:
        extra = f" ({r.reason})" if r.reason else ""
        errs = f" errors={len(r.stats.errors)}" if r.stats.errors else ""
        print(f"{r.source_id:<14}{r.status:<9}new={r.stats.items_new:<6}"
              f"obs={r.stats.observations_written:<6}req={r.stats.requests_made}{errs}{extra}")
    print(f"linked mentions: {linked}")
    return 1 if reports and all(r.status == "error" for r in reports) else 0


def cmd_link(_: argparse.Namespace) -> int:
    conn = open_db()
    seed_taxonomy(conn, load_taxonomy())
    print(f"linked mentions: {link_new_items(conn)}")
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    from liquidmexx.backfill import backfill_github_stars, backfill_hackernews

    settings = get_settings()
    with single_instance("collect") as acquired:
        if not acquired:
            print("a collection is running; try again shortly")
            return 1
        conn = open_db()
        if args.what == "github-stars":
            if not settings.github_token:
                print("warning: no GITHUB_TOKEN -- limited to 60 requests/hour; this will be slow")
            stats = backfill_github_stars(conn, max_repos=args.max_repos,
                                          max_stars=args.max_stars)
        else:
            stats = backfill_hackernews(conn, days=args.days)
        linked = link_new_items(conn)
    print(f"backfill {args.what}: items_new={stats.items_new} obs={stats.observations_written}"
          f" requests={stats.requests_made} errors={len(stats.errors)} linked={linked}")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    conn = open_db()
    q = conn.execute
    print("== database:", get_settings().db_path)
    for table in ("raw_items", "observations", "trends", "trend_mentions", "api_usage"):
        print(f"{table:<16}{q(f'SELECT COUNT(*) c FROM {table}').fetchone()['c']:>10}")
    print("\n== last runs")
    for r in q("SELECT source_id, mode, status, started_at, items_new, observations_written,"
               " requests_made, error FROM collection_runs WHERE id IN (SELECT MAX(id) FROM"
               " collection_runs GROUP BY source_id, mode) ORDER BY source_id"):
        err = f"  ! {r['error'][:90]}" if r["error"] else ""
        print(f"{r['source_id']:<14}{r['mode']:<9}{r['status']:<8}{r['started_at']}  "
              f"new={r['items_new']} obs={r['observations_written']} req={r['requests_made']}{err}")
    print("\n== spend today (UTC day) vs caps")
    guard = BudgetGuard(conn, load_budgets())
    for provider in ("x", "anthropic"):
        s = guard.status(provider)
        print(f"{provider:<10}${s.spent_usd:.4f} / ${s.cap_usd or 0:.2f}"
              f"  lifetime ${s.lifetime_spent_usd:.4f}"
              + (f"  credit left ${s.credit_remaining_usd:.2f}"
                 if s.credit_remaining_usd is not None else ""))
    print("\n== top trends by mentions, last 24h (excludes first-run catalog baselines)")
    for r in q("SELECT t.slug, COUNT(*) n, COUNT(DISTINCT m.source_id) src FROM trend_mentions m"
               " JOIN trends t ON t.id=m.trend_id JOIN v_raw_items r ON r.id=m.raw_item_id"
               " WHERE r.is_initial_snapshot=0 AND m.available_at >= strftime("
               "'%Y-%m-%dT%H:%M:%S.000000Z','now','-1 day') GROUP BY t.id ORDER BY n DESC"
               " LIMIT 15"):
        print(f"  {r['n']:>4} mentions  {r['src']} sources  {r['slug']}")
    print("\n(note: counts are raw evidence, not scores -- scoring arrives in Stage 4)")
    return 0


def cmd_backup(_: argparse.Namespace) -> int:
    conn = open_db()
    path = backup(conn, get_settings().data_dir / "backups", label="manual")
    print(f"backup written: {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="liquidmexx", description=__doc__)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--log-file", type=Path)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="create/upgrade the database").set_defaults(func=cmd_migrate)
    sub.add_parser("sources", help="list sources and readiness").set_defaults(func=cmd_sources)
    c = sub.add_parser("collect", help="run source adapters")
    c.add_argument("--due", action="store_true", help="only sources whose interval elapsed")
    c.add_argument("--source", action="append", choices=sorted(ADAPTERS))
    c.set_defaults(func=cmd_collect)
    sub.add_parser("link", help="link new items to trends").set_defaults(func=cmd_link)
    b = sub.add_parser("backfill", help="honest historical backfill")
    b.add_argument("what", choices=["github-stars", "hackernews"])
    b.add_argument("--days", type=int, default=30)
    b.add_argument("--max-repos", type=int, default=20)
    b.add_argument("--max-stars", type=int, default=2000)
    b.set_defaults(func=cmd_backfill)
    sub.add_parser("status", help="counts, last runs, spend").set_defaults(func=cmd_status)
    sub.add_parser("backup", help="snapshot the database").set_defaults(func=cmd_backup)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
    setup_logging(logging.DEBUG if args.verbose else logging.INFO,
                  str(args.log_file) if args.log_file else None)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
