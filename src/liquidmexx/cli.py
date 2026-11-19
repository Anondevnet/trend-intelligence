"""Command-line entry point: ``uv run liquidmexx <command>``."""

from __future__ import annotations

import argparse
import fcntl
import getpass
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from liquidmexx.backtest import Backtester
from liquidmexx.config import (
    load_backtest,
    load_budgets,
    load_publishing,
    load_scoring,
    load_sources,
    load_taxonomy,
    source_credibility,
)
from liquidmexx.costs.budget import BudgetGuard
from liquidmexx.db import backup, open_db
from liquidmexx.publishing import (
    DryRunTransport,
    Publisher,
    QueueError,
    approve,
    create_draft,
    engage_kill_switch,
    get_draft,
    kill_switch_engaged,
    list_drafts,
    reject,
    release_kill_switch,
    status_of,
)
from liquidmexx.publishing.queue import events as draft_events
from liquidmexx.redaction import setup_logging
from liquidmexx.scoring import SCORER_VERSION, Scorer, verify_run
from liquidmexx.settings import get_settings
from liquidmexx.sources import ADAPTERS
from liquidmexx.timeutil import normalize_iso, now_iso
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
    print("\n== collected history (live, non-baseline items; Stage 6 needs 7+ days everywhere)")
    history = list(q("SELECT source_id, COUNT(DISTINCT substr(available_at,1,10)) days,"
               " MIN(substr(available_at,1,10)) first_day, MAX(available_at) last_item"
               " FROM v_raw_items WHERE is_backfill=0 AND is_initial_snapshot=0"
               " GROUP BY source_id ORDER BY source_id"))
    if not history:
        print("  none yet: only first (baseline) runs so far -- is the launchd job installed?")
    for r in history:
        mark = "ready" if r["days"] >= 7 else f"{7 - r['days']} more day(s) needed"
        print(f"  {r['source_id']:<14}{r['days']:>3} day(s) since {r['first_day']}"
              f"  last {r['last_item']}  {mark}")
    run = q("SELECT id, as_of, config_hash FROM scoring_runs"
            " ORDER BY as_of DESC, id DESC LIMIT 1").fetchone()
    if run:
        print(f"\n== top scored trends (scoring run {run['id']}, as_of {run['as_of']})")
        for r in q("SELECT t.slug, s.score, s.confidence FROM trend_scores s JOIN trends t"
                   " ON t.id=s.trend_id WHERE s.run_id=? ORDER BY s.score DESC, t.slug LIMIT 10",
                   (run["id"],)):
            print(f"  {r['score']:6.1f}  conf {r['confidence']:.2f}  {r['slug']}")
    print("\n== top trends by mentions, last 24h (excludes first-run catalog baselines)")
    for r in q("SELECT t.slug, COUNT(*) n, COUNT(DISTINCT m.source_id) src FROM trend_mentions m"
               " JOIN trends t ON t.id=m.trend_id JOIN v_raw_items r ON r.id=m.raw_item_id"
               " WHERE r.is_initial_snapshot=0 AND m.available_at >= strftime("
               "'%Y-%m-%dT%H:%M:%S.000000Z','now','-1 day') GROUP BY t.id ORDER BY n DESC"
               " LIMIT 15"):
        print(f"  {r['n']:>4} mentions  {r['src']} sources  {r['slug']}")
    pcfg = load_publishing()
    print(f"\n== posting: mode={pcfg.mode}  kill_switch="
          f"{'ENGAGED' if kill_switch_engaged(pcfg, get_settings().data_dir) else 'off'}"
          "  live X transport: none installed")
    counts: dict[str, int] = {}
    for _draft, st in list_drafts(conn, include_closed=True):
        counts[st] = counts.get(st, 0) + 1
    print("  queue: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "empty"))
    print("\n(mention counts are raw evidence; scores come from `liquidmexx score`)")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    conn = open_db()
    if args.verify is not None:
        problems = verify_run(conn, args.verify)
        for line in problems[:20]:
            print("MISMATCH", line)
        print(f"verify run {args.verify}: " + ("OK, reproduced exactly" if not problems
                                               else f"{len(problems)} mismatches"))
        return 0 if not problems else 1
    as_of = normalize_iso(args.as_of) if args.as_of else now_iso()
    scorer = Scorer(conn, load_scoring(), source_credibility())
    results, excluded = scorer.score(as_of)
    print(f"as_of {as_of}  scorer {SCORER_VERSION}  config {scorer.hash}  "
          f"trends scored {len(results)}")
    print("excluded/discounted mentions: " + ", ".join(f"{k}={v}" for k, v in excluded.items()))
    print(f"{'score':>6} {'conf':>5} {'accel':>6} {'vol':>5} {'brd':>5} {'nov':>5}  trend")

    def fmt(v: float | None) -> str:
        return "  -  " if v is None else f"{v:5.2f}"

    for s in results[: args.top]:
        c = s.components
        print(f"{s.score:6.1f} {s.confidence:5.2f} {fmt(c['acceleration']):>6} {fmt(c['volume'])}"
              f" {fmt(c['breadth'])} {fmt(c['novelty'])}  {s.slug}")
    if not args.no_save:
        run_id = scorer.save(as_of, results)
        print("saved as scoring run", run_id if run_id is not None else "(already stored)")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    conn = open_db()
    cfg = load_backtest()
    bt = Backtester(conn, load_scoring(), source_credibility(), cfg)
    horizons = args.horizon or [int(h) for h in cfg["horizons_days"]]
    print(f"backtest config {bt.hash}  data watermark {bt.data_watermark()}")

    def f(v: object) -> str:
        return "   -  " if v is None else f"{v:6.3f}" if isinstance(v, float) else f"{v!s:>6}"

    for h in horizons:
        results = bt.run(h)
        dates = results[0].dates if results else []
        span = f"{dates[0][:10]}..{dates[-1][:10]}" if dates else "none"
        print(f"\n== horizon {h}d  eval dates {len(dates)} ({span})")
        for res in results:
            m = res.metrics
            if m["status"] != "ok":
                print(f"  {res.detector:<12} {m['status']}: {m['reason']}")
                continue
            wf = m["walk_forward"]
            wf_txt = (f"WF prec {f(wf['precision'])} rec {f(wf['recall'])} alerts {wf['alerts']}"
                      if wf["status"] == "ok" else f"WF {wf['status']}")
            print(f"  {res.detector:<12} base {f(m['base_rate'])}  P@5 {f(m['precision_at_5'])}"
                  f" ({m['hits_at_5']})  P@10 {f(m['precision_at_10'])}"
                  f"  rho {f(m['spearman_mean'])}  {wf_txt}")
        if results and results[0].metrics["status"] == "ok":
            m0 = results[0].metrics
            print(f"  label sources {m0['label_sources']}  positives {m0['positives']}/"
                  f"{m0['n_points']}  unseen emergers {m0['unseen_emergers']}"
                  f"  backfill share {f(m0['backfill_share_mean'])}")
        if not args.no_save:
            ids = [bt.save(r) for r in results]
            print(f"  saved backtest runs {ids}")
    return 0


def cmd_backup(_: argparse.Namespace) -> int:
    conn = open_db()
    path = backup(conn, get_settings().data_dir / "backups", label="manual")
    print(f"backup written: {path}")
    return 0


def _human() -> str:
    return f"human:{getpass.getuser()}"


def _parse_source(value: str) -> dict[str, object]:
    if value.startswith("raw:") and value[4:].isdigit():
        return {"raw_item_id": int(value[4:])}
    return {"url": value}


def cmd_queue(args: argparse.Namespace) -> int:
    conn = open_db()
    cfg = load_publishing()
    if args.action == "add":
        draft_id, gate = create_draft(
            conn, cfg, text=args.text, sources=[_parse_source(v) for v in args.source or []],
            claim_status=args.claim, sponsored=args.sponsored, actor=_human())
        verdict = "passed the gate, pending review" if gate.passed else "BLOCKED"
        print(f"draft {draft_id}: {verdict}")
        for reason in gate.reasons:
            print(f"  - {reason}")
        return 0 if gate.passed else 1
    if args.action == "list":
        for row, status in list_drafts(conn, include_closed=args.all):
            print(f"{row.id:>5}  {status:<18}{row.claim_status:<12}{row.text[:70]!r}")
        return 0
    draft = get_draft(conn, args.id)
    if draft is None:
        print(f"draft {args.id} does not exist")
        return 1
    if args.action == "show":
        print(f"draft {draft.id}  status={status_of(conn, draft.id)}  claim={draft.claim_status}"
              f"  sponsored={draft.sponsored}\n  hash {draft.content_hash}\n  sources"
              f" {draft.sources}\n---\n{draft.text}\n---")
        for e in draft_events(conn, draft.id):
            print(f"  {e['at']}  {e['event']:<18}{e['actor']:<20}{e['detail_json']}")
        return 0
    try:
        if args.action == "approve":
            if not sys.stdin.isatty():
                print("approval needs an interactive terminal; refusing")
                return 1
            print(f"---\n{draft.text}\n---\nsources: {draft.sources}  claim: {draft.claim_status}")
            typed = input(f"Type the draft id ({draft.id}) to approve this exact text: ")
            if typed.strip() != str(draft.id):
                print("not approved")
                return 1
            approve(conn, cfg, draft.id, _human(), note=args.note or "")
            print(f"draft {draft.id} approved (valid {cfg.approval_ttl_hours:g}h)")
        elif args.action == "reject":
            reject(conn, draft.id, _human(), args.reason)
            print(f"draft {draft.id} rejected")
        elif args.action == "publish":
            # Only the dry-run transport exists. In mode "off" or "live" this is blocked.
            pub = Publisher(conn, cfg, DryRunTransport(), data_dir=get_settings().data_dir,
                            live_opt_in=get_settings().live_posting_opt_in,
                            guard=BudgetGuard(conn, load_budgets()))
            out = pub.publish(draft.id, _human())
            print(f"draft {draft.id}: {'OK ' + out.reason if out.ok else 'BLOCKED: ' + out.reason}")
            return 0 if out.ok else 1
    except QueueError as exc:
        print(f"refused: {exc}")
        return 1
    return 0


def cmd_posting(args: argparse.Namespace) -> int:
    conn = open_db()
    cfg = load_publishing()
    data_dir = get_settings().data_dir
    if args.action == "kill":
        engage_kill_switch(conn, data_dir, _human(), args.reason or "")
        print("kill switch ENGAGED: every publish is blocked")
    elif args.action == "unkill":
        released = release_kill_switch(conn, data_dir, _human())
        print("kill-switch file removed" if released else "kill-switch file was not present")
    killed = "ENGAGED" if kill_switch_engaged(cfg, data_dir) else "off"
    print(f"mode={cfg.mode}  kill_switch={killed}"
          f"  live_opt_in={get_settings().live_posting_opt_in}  live_transport=none installed"
          f"  max_posts_per_day={cfg.max_posts_per_day}")
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
    s = sub.add_parser("score", help="score trends from stored history (no network)")
    s.add_argument("--as-of", help="ISO UTC timestamp to score at (default: now)")
    s.add_argument("--top", type=int, default=20)
    s.add_argument("--no-save", action="store_true", help="print only, do not store")
    s.add_argument("--verify", type=int, metavar="RUN_ID",
                   help="recompute a stored run and compare exactly")
    s.set_defaults(func=cmd_score)
    bt = sub.add_parser("backtest", help="point-in-time backtest of detectors (no network)")
    bt.add_argument("--horizon", type=int, action="append", help="days (repeatable)")
    bt.add_argument("--no-save", action="store_true")
    bt.set_defaults(func=cmd_backtest)
    qp = sub.add_parser("queue", help="post drafts: add, list, show, approve, reject, publish")
    qsub = qp.add_subparsers(dest="action", required=True)
    qa = qsub.add_parser("add", help="add a draft (runs the quality gate; never approves)")
    qa.add_argument("--text", required=True)
    qa.add_argument("--source", action="append", help="raw:<raw_item_id> or an https URL")
    qa.add_argument("--claim", choices=["verified", "speculative", "rumor"], required=True)
    qa.add_argument("--sponsored", action="store_true")
    ql = qsub.add_parser("list", help="open drafts (--all for closed ones too)")
    ql.add_argument("--all", action="store_true")
    for name, helptext in (("show", "draft text and its audit log"),
                           ("approve", "approve a draft (interactive only)"),
                           ("reject", "reject a draft or withdraw its approval"),
                           ("publish", "publish an approved draft (dry-run transport only)")):
        a = qsub.add_parser(name, help=helptext)
        a.add_argument("id", type=int)
        if name == "approve":
            a.add_argument("--note")
        if name == "reject":
            a.add_argument("--reason", required=True)
    qp.set_defaults(func=cmd_queue)
    pp = sub.add_parser("posting", help="posting switches: status, kill, unkill")
    pp.add_argument("action", choices=["status", "kill", "unkill"])
    pp.add_argument("--reason")
    pp.set_defaults(func=cmd_posting)
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
