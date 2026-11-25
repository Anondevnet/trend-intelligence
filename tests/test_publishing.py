"""Stage 10 groundwork: nothing can be posted without a standing human approval.

All tests are offline. "Live" tests use an in-memory fake transport or an httpx MockTransport.
"""

from __future__ import annotations

import io
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.test_scoring import World
from trend_intelligence import cli
from trend_intelligence.config import PublishingConfig, load_budgets, load_publishing
from trend_intelligence.costs.budget import BudgetGuard
from trend_intelligence.publishing import (
    DryRunTransport,
    Publisher,
    QueueError,
    approve,
    create_draft,
    engage_kill_switch,
    reject,
    release_kill_switch,
    status_of,
)
from trend_intelligence.publishing.gate import check_text
from trend_intelligence.publishing.queue import log_event
from trend_intelligence.settings import PROJECT_ROOT
from trend_intelligence.timeutil import parse_iso, to_iso

NOW = "2026-11-20T12:00:00.000000Z"
GOOD = "Three new MCP servers for Solana wallets were listed in the registry this week."


def later(hours: float, base: str = NOW) -> str:
    return to_iso(parse_iso(base) + timedelta(hours=hours))


def pcfg(**over: Any) -> PublishingConfig:
    data = load_publishing().model_dump()
    data.update(over)
    return PublishingConfig.model_validate(data)


@dataclass
class FakeLiveTransport:
    """Stands in for a real X client. Counts calls; never touches the network."""

    sent: list[str] = field(default_factory=list)
    fail: bool = False

    @property
    def live(self) -> bool:
        return True

    def send(self, text: str) -> str:
        self.sent.append(text)
        if self.fail:
            raise TimeoutError("network timeout")
        return f"x-{len(self.sent)}"


@pytest.fixture
def src(conn: sqlite3.Connection) -> int:
    return World(conn).item("hackernews", NOW)


def draft(conn: sqlite3.Connection, src: int, cfg: PublishingConfig | None = None,
          text: str = GOOD, **kw: Any) -> int:
    did, _gate = create_draft(conn, cfg or pcfg(), text=text, sources=kw.pop(
        "sources", [{"raw_item_id": src}]), claim_status=kw.pop("claim_status", "verified"),
        actor="system:test", now=kw.pop("now", NOW), **kw)
    return did


def approved(conn: sqlite3.Connection, src: int, cfg: PublishingConfig | None = None,
             **kw: Any) -> int:
    did = draft(conn, src, cfg, **kw)
    approve(conn, cfg or pcfg(), did, "human:tester", now=NOW)
    return did


def publisher(conn: sqlite3.Connection, tmp_path: Path, mode: str, transport: Any,
              *, opt_in: bool = False, **over: Any) -> Publisher:
    return Publisher(conn, pcfg(mode=mode, **over), transport, data_dir=tmp_path,
                     live_opt_in=opt_in, guard=BudgetGuard(conn, load_budgets()))


def live_ready(conn: sqlite3.Connection, tmp_path: Path, **over: Any
               ) -> tuple[Publisher, FakeLiveTransport]:
    t = FakeLiveTransport()
    return publisher(conn, tmp_path, "live", t, opt_in=True, **over), t


# ---------------------------------------------------------------- defaults


def test_shipped_config_is_off_and_draft_only() -> None:
    cfg = load_publishing()
    assert cfg.mode == "off" and not cfg.kill_switch
    assert cfg.require_human_approval
    assert not (cfg.allow_urls or cfg.allow_mentions or cfg.allow_cashtags)
    assert cfg.min_sources >= 1


def test_config_refuses_to_drop_human_approval() -> None:
    with pytest.raises(ValueError, match="require_human_approval"):
        pcfg(require_human_approval=False)
    with pytest.raises(ValueError):
        pcfg(mode=False)        # what an unquoted `mode: off` in YAML would produce


def test_live_opt_in_needs_the_exact_value(monkeypatch: pytest.MonkeyPatch) -> None:
    from trend_intelligence.settings import Settings
    for value, expected in (("enabled", True), ("true", False), ("1", False), ("", False)):
        monkeypatch.setenv("TREND_INTEL_LIVE_POSTING", value)
        assert Settings(_env_file=None).live_posting_opt_in is expected  # type: ignore[call-arg]


def test_only_one_module_can_write_to_x() -> None:
    """Reads live in sources/x.py (search only); the one write path is publishing/x_transport."""
    src_dir = PROJECT_ROOT / "src" / "trend_intelligence"
    allowed = {("sources", "x.py"), ("publishing", "x_transport.py")}
    for path in src_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for needle in ("api.twitter.com", "upload.twitter.com", "tweepy"):
            assert needle not in text, f"{path} references {needle}"
        if (path.parent.name, path.name) not in allowed:
            assert "api.x.com" not in text, f"{path} references the X API"
            users = {"cli.py", "publisher.py", "__init__.py"}  # cli builds it; others only name it
            assert "x_transport" not in text or path.name in users, path
    x = (src_dir / "sources" / "x.py").read_text(encoding="utf-8").lower()
    assert x.count("api.x.com") == 1 and "/2/tweets/search/recent" in x
    for forbidden in ('request("post"', 'method="post"', ".post(", '/2/tweets"', "/2/users",
                      "oauth1", "access_token_secret", "/likes", "/retweets", "/following"):
        assert forbidden not in x, f"x.py must stay read-only: found {forbidden}"
    w = (src_dir / "publishing" / "x_transport.py").read_text(encoding="utf-8").lower()
    assert w.count("api.x.com") == 1 and w.count(".post(") == 1
    for forbidden in ("/2/users", "/likes", "/retweets", "/following", "/media", ".delete(",
                      ".put(", "for attempt", "while true", "max_retries"):
        assert forbidden not in w, f"x_transport.py must only create one post: {forbidden}"


# ---------------------------------------------------- no post without approval


@pytest.mark.parametrize("mode", ["off", "dry_run", "live"])
def test_unapproved_draft_is_never_sent(conn: sqlite3.Connection, src: int, tmp_path: Path,
                                        mode: str) -> None:
    did = draft(conn, src)
    t = FakeLiveTransport() if mode == "live" else DryRunTransport()
    out = publisher(conn, tmp_path, mode, t, opt_in=True).publish(did, "system:test", now=NOW)
    assert not out.ok and t.sent == []
    assert status_of(conn, did) == "pending_review"


def test_mode_off_blocks_even_an_approved_draft(conn: sqlite3.Connection, src: int,
                                                tmp_path: Path) -> None:
    did = approved(conn, src)
    t = FakeLiveTransport()
    out = publisher(conn, tmp_path, "off", t, opt_in=True).publish(did, "x", now=NOW)
    assert not out.ok and "off" in out.reason and t.sent == []


def test_live_needs_both_switches_and_a_live_transport(conn: sqlite3.Connection, src: int,
                                                       tmp_path: Path) -> None:
    did = approved(conn, src)
    t = FakeLiveTransport()
    out = publisher(conn, tmp_path, "live", t, opt_in=False).publish(did, "x", now=NOW)
    assert "TREND_INTEL_LIVE_POSTING" in out.reason
    out = publisher(conn, tmp_path, "live", DryRunTransport(), opt_in=True).publish(
        did, "x", now=NOW)
    assert "no live X transport" in out.reason
    out = publisher(conn, tmp_path, "dry_run", t).publish(did, "x", now=NOW)
    assert "refuses a live transport" in out.reason
    assert t.sent == [] and status_of(conn, did) == "approved"


def test_rejection_withdraws_approval(conn: sqlite3.Connection, src: int,
                                      tmp_path: Path) -> None:
    did = approved(conn, src)
    reject(conn, did, "human:tester", "changed my mind", now=later(1))
    pub, t = live_ready(conn, tmp_path)
    assert not pub.publish(did, "x", now=later(2)).ok and t.sent == []
    with pytest.raises(QueueError):
        approve(conn, pcfg(), did, "human:tester", now=later(3))


def test_approval_expires(conn: sqlite3.Connection, src: int, tmp_path: Path) -> None:
    did = approved(conn, src)
    pub, t = live_ready(conn, tmp_path)
    out = pub.publish(did, "x", now=later(25))
    assert "expired" in out.reason and t.sent == []


def test_only_humans_can_approve(conn: sqlite3.Connection, src: int) -> None:
    did = draft(conn, src)
    for actor in ("system:llm", "sonnet", "human:"):
        with pytest.raises(QueueError, match="human"):
            approve(conn, pcfg(), did, actor, now=NOW)


def test_gate_failed_draft_cannot_be_approved(conn: sqlite3.Connection, src: int) -> None:
    did = draft(conn, src, text="SOL will hit $500 by EOY, buy now")
    assert status_of(conn, did) == "blocked_by_gate"
    with pytest.raises(QueueError):
        approve(conn, pcfg(), did, "human:tester", now=NOW)


# ------------------------------------------------ database-level guarantees


def test_db_refuses_publish_records_without_approval(conn: sqlite3.Connection,
                                                     src: int) -> None:
    did = draft(conn, src)
    h = conn.execute("SELECT content_hash FROM post_drafts WHERE id=?", (did,)).fetchone()[0]
    for event in ("publish_attempt", "published_live", "published_dry_run"):
        with pytest.raises(sqlite3.IntegrityError, match="approval"):
            log_event(conn, event, "attacker", draft_id=did, content_hash=h)


def test_db_refuses_bad_approvals(conn: sqlite3.Connection, src: int) -> None:
    did = draft(conn, src)
    h = conn.execute("SELECT content_hash FROM post_drafts WHERE id=?", (did,)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="human"):
        log_event(conn, "approved", "system:auto", draft_id=did, content_hash=h)
    with pytest.raises(sqlite3.IntegrityError, match="content"):
        log_event(conn, "approved", "human:x", draft_id=did, content_hash="0" * 64)
    bad = draft(conn, src, text="Price target $9k, guaranteed")
    hb = conn.execute("SELECT content_hash FROM post_drafts WHERE id=?", (bad,)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="quality gate"):
        log_event(conn, "approved", "human:x", draft_id=bad, content_hash=hb)


def test_db_refuses_publish_after_rejection(conn: sqlite3.Connection, src: int) -> None:
    did = approved(conn, src)
    reject(conn, did, "human:tester", "no", now=later(1))
    h = conn.execute("SELECT content_hash FROM post_drafts WHERE id=?", (did,)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="approval"):
        log_event(conn, "publish_attempt", "attacker", draft_id=did, content_hash=h)


def test_drafts_and_audit_log_are_append_only(conn: sqlite3.Connection, src: int) -> None:
    approved(conn, src)
    for sql in ("UPDATE post_drafts SET text='edited'", "DELETE FROM post_drafts",
                "UPDATE post_events SET event='approved'", "DELETE FROM post_events"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(sql)


# ------------------------------------------------------- kill switch


def test_kill_switch_blocks_and_is_logged(conn: sqlite3.Connection, src: int,
                                          tmp_path: Path) -> None:
    did = approved(conn, src)
    pub, t = live_ready(conn, tmp_path)
    engage_kill_switch(conn, tmp_path, "human:tester", "test")
    assert "kill switch" in pub.publish(did, "x", now=NOW).reason and t.sent == []
    assert release_kill_switch(conn, tmp_path, "human:tester")
    pub_cfg_kill, t2 = live_ready(conn, tmp_path, kill_switch=True)
    assert "kill switch" in pub_cfg_kill.publish(did, "x", now=NOW).reason and t2.sent == []
    logged = [r[0] for r in conn.execute(
        "SELECT event FROM post_events WHERE draft_id IS NULL ORDER BY id")]
    assert logged == ["kill_switch_on", "kill_switch_off"]


# ------------------------------------------------- the one allowed path


def test_approved_draft_publishes_once_and_is_audited(conn: sqlite3.Connection, src: int,
                                                      tmp_path: Path) -> None:
    did = approved(conn, src)
    pub, t = live_ready(conn, tmp_path)
    out = pub.publish(did, "human:tester", now=later(1))
    assert out.ok and out.external_id == "x-1" and t.sent == [GOOD]
    assert status_of(conn, did) == "published_live"
    again = pub.publish(did, "human:tester", now=later(2))
    assert not again.ok and len(t.sent) == 1
    trail = [r[0] for r in conn.execute(
        "SELECT event FROM post_events WHERE draft_id=? ORDER BY id", (did,))]
    assert trail == ["created", "gate_passed", "approved", "publish_attempt",
                     "published_live", "publish_blocked"]
    spend = conn.execute("SELECT SUM(cost_usd) FROM api_usage WHERE provider='x'").fetchone()[0]
    assert spend == pytest.approx(0.015)


def test_dry_run_records_without_spending(conn: sqlite3.Connection, src: int,
                                          tmp_path: Path) -> None:
    did = approved(conn, src)
    t = DryRunTransport()
    out = publisher(conn, tmp_path, "dry_run", t).publish(did, "human:tester", now=NOW)
    assert out.ok and t.sent == [GOOD] and status_of(conn, did) == "published_dry_run"
    assert conn.execute("SELECT COUNT(*) FROM api_usage WHERE provider='x'").fetchone()[0] == 0


def test_transport_failure_is_terminal_no_retry(conn: sqlite3.Connection, src: int,
                                                tmp_path: Path) -> None:
    did = approved(conn, src)
    pub, t = live_ready(conn, tmp_path)
    t.fail = True
    out = pub.publish(did, "x", now=NOW)
    assert not out.ok and status_of(conn, did) == "publish_failed"
    t.fail = False
    assert not pub.publish(did, "x", now=later(1)).ok and len(t.sent) == 1


def test_daily_cap_and_x_budget(conn: sqlite3.Connection, src: int, tmp_path: Path) -> None:
    pub, t = live_ready(conn, tmp_path, max_posts_per_day=1)
    first = approved(conn, src, text=GOOD)
    second = approved(conn, src, text="Hugging Face trending now lists four agent frameworks.")
    assert pub.publish(first, "x", now=NOW).ok
    assert "cap" in pub.publish(second, "x", now=NOW).reason and len(t.sent) == 1
    guard = BudgetGuard(conn, load_budgets())
    guard.record("x", "test", "fill", cost_usd=0.14, ts=NOW)
    pub2, t2 = live_ready(conn, tmp_path)
    assert "budget" in pub2.publish(second, "x", now=NOW).reason and t2.sent == []


def test_duplicate_of_published_post_is_blocked(conn: sqlite3.Connection, src: int,
                                                tmp_path: Path) -> None:
    did = approved(conn, src)
    assert publisher(conn, tmp_path, "dry_run", DryRunTransport()).publish(
        did, "x", now=NOW).ok
    dup = draft(conn, src, text=GOOD.upper() + "!", now=later(1))
    assert status_of(conn, dup) == "blocked_by_gate"


# -------------------------------------------------------------- quality gate


@pytest.mark.parametrize(("text", "code"), [
    ("New Solana MCP server released, details at https://example.com/x", "url"),
    ("Docs are live on example.io for the new agent SDK", "url"),
    ("Big week for agents, thanks @someone for the tip", "mention"),
    ("$BONK volume is up on the week across DEXes", "cashtag"),
    ("Agent tokens are the next 100x play this cycle", "financial_advice"),
    ("Not financial advice but agent infra looks strong", "financial_advice"),
    ("Buy the dip on AI coins while you can", "financial_advice"),
    ("SOL could hit 500 after the ETF decision", "price_prediction"),
    ("Price target for the agent sector is up again", "price_prediction"),
    ("ETH to $10k by Christmas says one analyst", "price_prediction"),
    ("x" * 281, "length"),
    ("   ", "empty"),
])
def test_gate_blocks(text: str, code: str) -> None:
    reasons = check_text(text, pcfg(), claim_status="verified", sponsored=False)
    assert any(r.startswith(code) for r in reasons), reasons


def test_gate_requires_labels_for_speculation_and_sponsorship() -> None:
    cfg = pcfg()
    assert any(r.startswith("uncertainty") for r in check_text(
        "A big exchange is adding agent tokens", cfg, claim_status="rumor", sponsored=False))
    assert not check_text("Unconfirmed: a big exchange is adding agent tokens", cfg,
                          claim_status="rumor", sponsored=False)
    assert any(r.startswith("sponsored") for r in check_text(
        GOOD, cfg, claim_status="verified", sponsored=True))
    assert not check_text(GOOD + " #ad", cfg, claim_status="verified", sponsored=True)


def test_gate_requires_real_sources(conn: sqlite3.Connection, src: int) -> None:
    assert status_of(conn, draft(conn, src, sources=[])) == "blocked_by_gate"
    assert status_of(conn, draft(conn, src, sources=[{"raw_item_id": 99999}])) == \
        "blocked_by_gate"
    assert status_of(conn, draft(conn, src, sources=[{"url": "not a url"}])) == \
        "blocked_by_gate"
    ok = draft(conn, src, sources=[{"url": "https://example.com/post"}])
    assert status_of(conn, ok) == "pending_review"


def test_clean_sourced_draft_passes(conn: sqlite3.Connection, src: int) -> None:
    did = draft(conn, src)
    row = conn.execute("SELECT detail_json FROM post_events WHERE draft_id=? AND"
                       " event='gate_passed'", (did,)).fetchone()
    assert json.loads(row[0]) == {"reasons": []}


def test_approval_binds_to_exact_content(conn: sqlite3.Connection, src: int) -> None:
    a = draft(conn, src)
    b = draft(conn, src, text=GOOD + " More soon.", supersedes=a)
    approve(conn, pcfg(), a, "human:tester", now=NOW)
    assert status_of(conn, a) == "approved" and status_of(conn, b) == "pending_review"


# ---------------------------------------------------------------- CLI


def test_cli_approve_refuses_without_terminal(conn: sqlite3.Connection, src: int,
                                              monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    did = draft(conn, src)
    monkeypatch.setattr(cli, "open_db", lambda: conn)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("1\n"))
    assert cli.main(["queue", "approve", str(did)]) == 1
    assert "interactive" in capsys.readouterr().out
    assert status_of(conn, did) == "pending_review"


def test_cli_publish_with_shipped_config_is_blocked(conn: sqlite3.Connection, src: int,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    did = approved(conn, src)
    monkeypatch.setattr(cli, "open_db", lambda: conn)
    assert cli.main(["queue", "publish", str(did)]) == 1     # shipped mode is "off"
    assert status_of(conn, did) == "approved"
