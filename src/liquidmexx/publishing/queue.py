"""Approval queue: immutable drafts plus an append-only event log (the audit log).

A draft's state is derived from its events. Only a ``human:<name>`` actor can approve,
and approval binds to the draft's content hash; SQLite triggers (migration 0007) refuse
any approval or publish record that breaks those rules, even if this module is bypassed.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from liquidmexx.config import PublishingConfig
from liquidmexx.publishing.gate import GateResult, run_gate
from liquidmexx.timeutil import now_iso


class QueueError(RuntimeError):
    """An action that the draft's current state does not allow."""


@dataclass(frozen=True)
class Draft:
    id: int
    created_at: str
    created_by: str
    text: str
    content_hash: str
    claim_status: str
    sponsored: bool
    sources: list[dict[str, Any]]
    trend_id: int | None
    supersedes: int | None


def content_hash(text: str, sources: Sequence[dict[str, Any]], claim_status: str,
                 sponsored: bool) -> str:
    blob = json.dumps({"text": text, "sources": list(sources), "claim_status": claim_status,
                       "sponsored": bool(sponsored)}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _require_human(actor: str) -> None:
    if not actor.startswith("human:") or len(actor) <= len("human:"):
        raise QueueError("only a human actor (human:<name>) can approve or reject")


def log_event(conn: sqlite3.Connection, event: str, actor: str, *, draft_id: int | None = None,
              content_hash: str | None = None, detail: dict[str, Any] | None = None,
              now: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO post_events(at, draft_id, event, actor, content_hash, detail_json)"
        " VALUES (?,?,?,?,?,?)",
        (now or now_iso(), draft_id, event, actor, content_hash,
         json.dumps(detail or {}, sort_keys=True)))
    return int(cur.lastrowid or 0)


def get_draft(conn: sqlite3.Connection, draft_id: int) -> Draft | None:
    r = conn.execute("SELECT * FROM post_drafts WHERE id=?", (draft_id,)).fetchone()
    if r is None:
        return None
    return Draft(id=r["id"], created_at=r["created_at"], created_by=r["created_by"],
                 text=r["text"], content_hash=r["content_hash"], claim_status=r["claim_status"],
                 sponsored=bool(r["sponsored"]), sources=json.loads(r["sources_json"]),
                 trend_id=r["trend_id"], supersedes=r["supersedes"])


def events(conn: sqlite3.Connection, draft_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM post_events WHERE draft_id=? ORDER BY id",
                        (draft_id,)).fetchall()


def status_of(conn: sqlite3.Connection, draft_id: int) -> str:
    """new | pending_review | blocked_by_gate | approved | rejected | publishing |
    published_dry_run | published_live | publish_failed"""
    status = "new"
    for e in events(conn, draft_id):
        ev = e["event"]
        if ev == "gate_passed" and status in ("new", "blocked_by_gate"):
            status = "pending_review"
        elif ev == "gate_failed" and status in ("new", "pending_review"):
            status = "blocked_by_gate"
        elif ev in ("approved", "rejected", "published_dry_run", "published_live",
                    "publish_failed"):
            status = ev
        elif ev == "publish_attempt":
            status = "publishing"
    return status


def last_approval(conn: sqlite3.Connection, draft_id: int) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM post_events WHERE draft_id=? AND event='approved' ORDER BY id DESC"
        " LIMIT 1", (draft_id,)).fetchone()
    return row


def create_draft(conn: sqlite3.Connection, cfg: PublishingConfig, *, text: str,
                 sources: Sequence[dict[str, Any]], claim_status: str, actor: str,
                 sponsored: bool = False, trend_id: int | None = None,
                 supersedes: int | None = None, meta: dict[str, Any] | None = None,
                 now: str | None = None) -> tuple[int, GateResult]:
    """Store a draft and run the gate on it. Never approves anything."""
    when = now or now_iso()
    text = text.strip()
    chash = content_hash(text, sources, claim_status, sponsored)
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO post_drafts(created_at, created_by, text, content_hash, claim_status,"
            " sponsored, sources_json, trend_id, supersedes, meta_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (when, actor, text, chash, claim_status, int(sponsored),
             json.dumps(list(sources), sort_keys=True), trend_id, supersedes,
             json.dumps(meta or {}, sort_keys=True)))
        draft_id = int(cur.lastrowid or 0)
        log_event(conn, "created", actor, draft_id=draft_id, content_hash=chash, now=when)
        gate = run_gate(conn, cfg, text=text, sources=sources, claim_status=claim_status,
                        sponsored=sponsored, now=when, draft_id=draft_id)
        log_event(conn, "gate_passed" if gate.passed else "gate_failed", "system:gate",
                  draft_id=draft_id, content_hash=chash, detail={"reasons": gate.reasons},
                  now=when)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return draft_id, gate


def approve(conn: sqlite3.Connection, cfg: PublishingConfig, draft_id: int, actor: str,
            *, note: str = "", now: str | None = None) -> None:
    """Record a human approval of the draft's exact current content.

    Re-runs the gate first; a draft that no longer passes is marked blocked instead.
    """
    _require_human(actor)
    when = now or now_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        draft = get_draft(conn, draft_id)
        if draft is None:
            raise QueueError(f"draft {draft_id} does not exist")
        status = status_of(conn, draft_id)
        if status != "pending_review":
            raise QueueError(f"draft {draft_id} is {status}; only pending_review can be approved")
        gate = run_gate(conn, cfg, text=draft.text, sources=draft.sources,
                        claim_status=draft.claim_status, sponsored=draft.sponsored,
                        now=when, draft_id=draft_id)
        if not gate.passed:
            log_event(conn, "gate_failed", "system:gate", draft_id=draft_id,
                      content_hash=draft.content_hash, detail={"reasons": gate.reasons},
                      now=when)
            conn.execute("COMMIT")
            raise QueueError(f"draft {draft_id} no longer passes the gate: {gate.reasons}")
        log_event(conn, "approved", actor, draft_id=draft_id, content_hash=draft.content_hash,
                  detail={"note": note}, now=when)
        conn.execute("COMMIT")
    except QueueError:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise


def reject(conn: sqlite3.Connection, draft_id: int, actor: str, reason: str,
           *, now: str | None = None) -> None:
    """Reject a draft (also withdraws an earlier approval). Terminal."""
    _require_human(actor)
    draft = get_draft(conn, draft_id)
    if draft is None:
        raise QueueError(f"draft {draft_id} does not exist")
    status = status_of(conn, draft_id)
    if status in ("publishing", "published_dry_run", "published_live", "publish_failed",
                  "rejected"):
        raise QueueError(f"draft {draft_id} is {status}; it cannot be rejected now")
    log_event(conn, "rejected", actor, draft_id=draft_id, content_hash=draft.content_hash,
              detail={"reason": reason}, now=now)


def list_drafts(conn: sqlite3.Connection, *, include_closed: bool = False
                ) -> list[tuple[Draft, str]]:
    out: list[tuple[Draft, str]] = []
    for r in conn.execute("SELECT id FROM post_drafts ORDER BY id"):
        draft = get_draft(conn, r["id"])
        assert draft is not None
        status = status_of(conn, draft.id)
        if include_closed or status in ("pending_review", "approved", "blocked_by_gate",
                                        "publishing"):
            out.append((draft, status))
    return out
