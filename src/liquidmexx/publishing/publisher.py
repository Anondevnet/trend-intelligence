"""The only path from an approved draft to a transport. Fails closed at every step.

There is deliberately no live X transport in this codebase. ``DryRunTransport`` never
touches the network. A live transport will be added in Stage 10 proper, only with
explicit approval, and even then ``publish`` refuses unless every switch is on.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Protocol

from liquidmexx.config import PublishingConfig
from liquidmexx.costs.budget import BudgetExceededError, BudgetGuard
from liquidmexx.publishing.gate import URL_RE, run_gate
from liquidmexx.publishing.queue import get_draft, last_approval, log_event, status_of
from liquidmexx.redaction import redact
from liquidmexx.timeutil import now_iso, parse_iso

log = logging.getLogger(__name__)

KILL_SWITCH_FILE = "POSTING_KILL_SWITCH"


class Transport(Protocol):
    @property
    def live(self) -> bool: ...

    def send(self, text: str) -> str:
        """Publish ``text``; return the external post id."""
        ...


@dataclass
class DryRunTransport:
    """Records what would have been posted. No network, no credentials."""

    sent: list[str] = field(default_factory=list)

    @property
    def live(self) -> bool:
        return False

    def send(self, text: str) -> str:
        self.sent.append(text)
        return f"dryrun-{len(self.sent)}"


@dataclass(frozen=True)
class PublishOutcome:
    ok: bool
    reason: str
    external_id: str | None = None


def kill_switch_path(data_dir: Path) -> Path:
    return data_dir / KILL_SWITCH_FILE


def kill_switch_engaged(cfg: PublishingConfig, data_dir: Path) -> bool:
    return cfg.kill_switch or kill_switch_path(data_dir).exists()


def engage_kill_switch(conn: sqlite3.Connection, data_dir: Path, actor: str,
                       reason: str = "") -> None:
    path = kill_switch_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"engaged by {actor} at {now_iso()}: {reason}\n", encoding="utf-8")
    log_event(conn, "kill_switch_on", actor, detail={"reason": reason})


def release_kill_switch(conn: sqlite3.Connection, data_dir: Path, actor: str) -> bool:
    """Remove the kill-switch file. Does not enable posting (mode still applies)."""
    path = kill_switch_path(data_dir)
    if not path.exists():
        return False
    path.unlink()
    log_event(conn, "kill_switch_off", actor)
    return True


class Publisher:
    def __init__(self, conn: sqlite3.Connection, cfg: PublishingConfig, transport: Transport,
                 *, data_dir: Path, live_opt_in: bool = False,
                 guard: BudgetGuard | None = None) -> None:
        self.conn = conn
        self.cfg = cfg
        self.transport = transport
        self.data_dir = data_dir
        self.live_opt_in = live_opt_in
        self.guard = guard

    @property
    def mode(self) -> str:
        return self.cfg.mode

    def _cost(self, text: str) -> float:
        return self.cfg.x_cost_usd["post_with_url" if URL_RE.search(text) else "post"]

    def _preflight(self, draft_id: int, now: str) -> str | None:
        """First reason this draft cannot be published now, or None."""
        if kill_switch_engaged(self.cfg, self.data_dir):
            return "kill switch is engaged"
        if self.mode == "off":
            return "posting mode is off (config/publishing.yaml)"
        if self.mode == "dry_run" and self.transport.live:
            return "dry_run mode refuses a live transport"
        if self.mode == "live":
            if not self.live_opt_in:
                return "live mode needs LIQUIDMEXX_LIVE_POSTING=enabled"
            if not self.transport.live:
                return "live mode but no live X transport is installed"
            if self.guard is None:
                return "live mode needs a budget guard"
        draft = get_draft(self.conn, draft_id)
        assert draft is not None
        status = status_of(self.conn, draft_id)
        if status != "approved":
            return f"draft is {status}, not approved"
        approval = last_approval(self.conn, draft_id)
        if approval is None or approval["content_hash"] != draft.content_hash:
            return "no approval for this exact content"
        age = parse_iso(now) - parse_iso(approval["at"])
        if age > timedelta(hours=self.cfg.approval_ttl_hours):
            return f"approval expired ({self.cfg.approval_ttl_hours:g}h limit)"
        if age < timedelta(0):
            return "approval is dated in the future"
        gate = run_gate(self.conn, self.cfg, text=draft.text, sources=draft.sources,
                        claim_status=draft.claim_status, sponsored=draft.sponsored, now=now,
                        draft_id=draft_id)
        if not gate.passed:
            return "quality gate: " + "; ".join(gate.reasons)
        attempts = self.conn.execute(
            "SELECT COUNT(*) FROM post_events WHERE event='publish_attempt'"
            " AND substr(at,1,10)=? AND json_extract(detail_json,'$.mode')=?",
            (now[:10], self.mode)).fetchone()[0]
        if attempts >= self.cfg.max_posts_per_day:
            return f"daily post cap reached ({self.cfg.max_posts_per_day})"
        return None

    def publish(self, draft_id: int, actor: str, *, now: str | None = None) -> PublishOutcome:
        when = now or now_iso()
        draft = get_draft(self.conn, draft_id)
        if draft is None:
            return PublishOutcome(False, f"draft {draft_id} does not exist")
        detail: dict[str, object] = {"mode": self.mode}
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            reason = self._preflight(draft_id, when)
            cost = self._cost(draft.text)
            if reason is None and self.mode == "live":
                assert self.guard is not None
                try:
                    self.guard.check("x", cost, when)
                except BudgetExceededError as exc:
                    reason = f"budget: {exc}"
            if reason is not None:
                log_event(self.conn, "publish_blocked", actor, draft_id=draft_id,
                          content_hash=draft.content_hash, detail={**detail, "reason": reason},
                          now=when)
                self.conn.execute("COMMIT")
                log.info("publish of draft %s blocked: %s", draft_id, reason)
                return PublishOutcome(False, reason)
            # Committed before sending: if the process dies mid-send the draft stays
            # 'publishing' and can never be retried into a double post.
            log_event(self.conn, "publish_attempt", actor, draft_id=draft_id,
                      content_hash=draft.content_hash, detail=detail, now=when)
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        try:
            external_id = self.transport.send(draft.text)
        except Exception as exc:  # any transport failure is recorded, never retried
            msg = redact(f"{type(exc).__name__}: {exc}")
            log_event(self.conn, "publish_failed", actor, draft_id=draft_id,
                      content_hash=draft.content_hash, detail={**detail, "error": msg}, now=when)
            return PublishOutcome(False, f"transport failed: {msg}")
        event = "published_live" if self.mode == "live" else "published_dry_run"
        log_event(self.conn, event, actor, draft_id=draft_id, content_hash=draft.content_hash,
                  detail={**detail, "external_id": external_id}, now=when)
        if self.mode == "live" and self.guard is not None:
            self.guard.record("x", "POST /2/tweets", "create_post", cost_usd=cost,
                              detail=json.dumps({"draft_id": draft_id}), ts=when)
        return PublishOutcome(True, event, external_id)
