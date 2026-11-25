"""Per-tier caps (with the provider cap, in one locked transaction) and migration 0004."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from trend_intelligence.config import BudgetsConfig
from trend_intelligence.costs.budget import BudgetExceededError, BudgetGuard, LLMUsage
from trend_intelligence.db import MIGRATIONS_DIR, connect, migrate


def anthropic_guard(conn: sqlite3.Connection) -> BudgetGuard:
    return BudgetGuard(conn, BudgetsConfig.model_validate({"providers": {"anthropic": {
        "daily_cap_usd": 2.00, "tiers": {"opus": 1.30, "sonnet": 0.50, "haiku": 0.20}}}}))


def usage_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM api_usage").fetchone()[0])


def test_call_over_tier_cap_is_refused_and_not_logged(conn: sqlite3.Connection) -> None:
    g = anthropic_guard(conn)
    g.charge("anthropic", "/v1/messages", "post_draft", 0.45, tier="sonnet",
             llm=LLMUsage("claude-sonnet-5-5", input_tokens=10_000, output_tokens=43_000))
    with pytest.raises(BudgetExceededError, match="tier cap"):
        g.charge("anthropic", "/v1/messages", "post_draft", 0.10, tier="sonnet")
    assert usage_rows(conn) == 1
    # Same amount still fits under a different tier and under the provider cap.
    g.charge("anthropic", "/v1/messages", "deep_research", 0.10, tier="opus")
    assert g.spent_today("anthropic", tier="sonnet") == pytest.approx(0.45)
    assert g.spent_today("anthropic") == pytest.approx(0.55)


def test_provider_cap_still_applies_across_tiers(conn: sqlite3.Connection) -> None:
    g = BudgetGuard(conn, BudgetsConfig.model_validate({"providers": {"anthropic": {
        "daily_cap_usd": 1.00, "tiers": {"opus": 0.80, "haiku": 0.20}}}}))
    g.charge("anthropic", "/v1/messages", "x", 0.80, tier="opus")
    g.charge("anthropic", "/v1/messages", "x", 0.15, tier="haiku")
    with pytest.raises(BudgetExceededError):
        g.charge("anthropic", "/v1/messages", "x", 0.06, tier="haiku")  # tier would allow 0.05


def test_paid_call_must_name_a_configured_tier(conn: sqlite3.Connection) -> None:
    g = anthropic_guard(conn)
    with pytest.raises(BudgetExceededError, match="must name"):
        g.charge("anthropic", "/v1/messages", "x", 0.01)
    with pytest.raises(BudgetExceededError, match="no daily cap for tier"):
        g.charge("anthropic", "/v1/messages", "x", 0.01, tier="fable")
    assert usage_rows(conn) == 0


def test_token_columns_are_recorded(conn: sqlite3.Connection) -> None:
    g = anthropic_guard(conn)
    g.charge("anthropic", "/v1/messages/batches", "deep_research", 0.05, tier="opus",
             llm=LLMUsage("claude-opus-5-5", 1000, 200, 5000, 300, is_batch=True))
    row = conn.execute("SELECT * FROM api_usage").fetchone()
    assert (row["tier"], row["model"], row["input_tokens"], row["output_tokens"],
            row["cache_read_tokens"], row["cache_write_tokens"], row["is_batch"]) == (
        "opus", "claude-opus-5-5", 1000, 200, 5000, 300, 1)


def test_migration_0004_keeps_old_rows_and_backs_up(tmp_path: Path) -> None:
    old_dir = tmp_path / "old_migrations"
    old_dir.mkdir()
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if f.name < "0004":
            shutil.copy(f, old_dir / f.name)
    db = tmp_path / "t.db"
    c = connect(db)
    migrate(c, old_dir)
    c.execute("INSERT INTO api_usage(ts, utc_day, provider, endpoint, operation, units,"
              " cost_usd, status_code, detail) VALUES"
              " ('2026-11-18T10:00:00.000000Z','2026-11-18','github','/search','read',1,0,200,'a'),"
              " ('2026-11-18T11:00:00.000000Z','2026-11-18','x','/2/tweets','post_create',1,"
              "0.015,201,'b')")
    cols = "id, ts, utc_day, provider, endpoint, operation, units, cost_usd, status_code, detail"
    before = [tuple(r) for r in c.execute(f"SELECT {cols} FROM api_usage ORDER BY id")]

    backups = tmp_path / "backups"
    assert "0004_llm_usage" in migrate(c, MIGRATIONS_DIR, backups)
    after = [tuple(r) for r in c.execute(f"SELECT {cols} FROM api_usage ORDER BY id")]
    assert after == before
    new = c.execute("SELECT tier, model, input_tokens, is_batch FROM api_usage").fetchall()
    assert all(tuple(r) == (None, None, None, 0) for r in new)
    assert len(list(backups.glob("*pre-migrate*.db"))) == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        c.execute("UPDATE api_usage SET tier='opus'")
