from __future__ import annotations

import logging
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from liquidmexx.config import BudgetsConfig, load_budgets
from liquidmexx.costs.budget import BudgetExceededError, BudgetGuard
from liquidmexx.redaction import REDACTED, RedactingFilter, redact
from liquidmexx.settings import PROJECT_ROOT, Settings


def guard(conn: sqlite3.Connection, cap: float = 0.15) -> BudgetGuard:
    return BudgetGuard(conn, BudgetsConfig.model_validate(
        {"providers": {"x": {"daily_cap_usd": cap, "initial_credit_usd": 5.0},
                       "github": {"daily_cap_usd": 0.0}}}))


def test_configured_caps_match_decisions() -> None:
    b = load_budgets()
    assert b.providers["x"].daily_cap_usd == 0.15
    assert b.providers["anthropic"].daily_cap_usd == 2.00


def test_daily_cap_blocks_overspend(conn: sqlite3.Connection) -> None:
    g = guard(conn)
    for _ in range(10):
        g.charge("x", "/2/tweets", "post_create", 0.015)
    assert g.spent_today("x") == pytest.approx(0.15)
    with pytest.raises(BudgetExceededError):
        g.charge("x", "/2/tweets", "post_create", 0.015)
    assert conn.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0] == 10  # refused not logged


def test_single_url_post_exceeds_daily_x_cap(conn: sqlite3.Connection) -> None:
    with pytest.raises(BudgetExceededError):
        guard(conn).charge("x", "/2/tweets", "post_create_with_url", 0.20)


def test_unbudgeted_paid_provider_is_refused(conn: sqlite3.Connection) -> None:
    g = guard(conn)
    with pytest.raises(BudgetExceededError, match="no budget"):
        g.check("newvendor", 0.01)
    g.check("newvendor", 0.0)  # free calls fine


def test_free_provider_records_requests_without_cost(conn: sqlite3.Connection) -> None:
    g = guard(conn)
    g.record("github", "/search", "read")
    assert g.spent_today("github") == 0.0
    with pytest.raises(BudgetExceededError):
        g.charge("github", "/x", "read", 0.01)


def test_status_reports_remaining_credit(conn: sqlite3.Connection) -> None:
    g = guard(conn)
    g.charge("x", "/2/tweets", "post_create", 0.015)
    s = g.status("x")
    assert s.remaining_today_usd == pytest.approx(0.135)
    assert s.credit_remaining_usd == pytest.approx(4.985)


def test_api_usage_ledger_is_append_only(conn: sqlite3.Connection) -> None:
    guard(conn).record("github", "/a", "read")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM api_usage")


def test_redaction_of_known_and_configured_secrets() -> None:
    fake_key = "sk-ant-" + "a" * 30
    fake_gh = "ghp_" + "b" * 36
    text = (f"key={fake_key} token {fake_gh} "
            "Authorization: Bearer abcdefghijklmnopqrstuv mysecret123")
    out = redact(text, ["mysecret123"])
    for s in (fake_key, fake_gh, "abcdefghijklmnopqrstuv", "mysecret123"):
        assert s not in out
    assert REDACTED in out


def test_logging_filter_redacts(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("t")
    logger.addFilter(RedactingFilter(["supersecretvalue"]))
    with caplog.at_level(logging.INFO):
        logger.info("calling with %s", "supersecretvalue")
    assert "supersecretvalue" not in caplog.text


def test_settings_never_expose_secret_values() -> None:
    s = Settings(_env_file=None, github_token="ghp_" + "c" * 36)  # type: ignore[call-arg]
    assert "ccccc" not in repr(s) and "ccccc" not in str(s.model_dump())
    assert s.credential_status()["github_token"] is True


def test_secret_scan_passes_on_repo() -> None:
    res = subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "secret_scan.py")],  # noqa: S603
                         capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stdout


def test_secret_scan_detects_planted_key(tmp_path: Path) -> None:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    try:
        import secret_scan
    finally:
        sys.path.pop(0)
    planted = "sk-ant-" + "Z" * 40
    assert any(p.search(f"ANTHROPIC_API_KEY={planted}") for p in secret_scan.PATTERNS.values())
