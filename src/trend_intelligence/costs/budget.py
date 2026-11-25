"""Append-only cost ledger and hard daily spending caps.

Every paid call must go through ``BudgetGuard.charge`` (check-then-record in a
single transaction). Free sources call ``record`` so request volume is visible.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from trend_intelligence.config import BudgetsConfig
from trend_intelligence.timeutil import now_iso

log = logging.getLogger(__name__)


class BudgetExceededError(RuntimeError):
    """Raised before a call that would push today's spend over the cap."""


@dataclass(frozen=True)
class LLMUsage:
    """Token accounting for one model call, stored alongside its cost."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    is_batch: bool = False


@dataclass(frozen=True)
class SpendStatus:
    provider: str
    utc_day: str
    spent_usd: float
    cap_usd: float | None
    remaining_today_usd: float | None
    lifetime_spent_usd: float
    credit_remaining_usd: float | None


class BudgetGuard:
    def __init__(self, conn: sqlite3.Connection, budgets: BudgetsConfig) -> None:
        self.conn = conn
        self.budgets = budgets

    @staticmethod
    def _day(ts: str) -> str:
        return ts[:10]

    def spent_today(
        self, provider: str, ts: str | None = None, *, tier: str | None = None
    ) -> float:
        """Today's spend for a provider, or for one tier of it when ``tier`` is given."""
        day = self._day(ts or now_iso())
        sql = "SELECT COALESCE(SUM(cost_usd),0) AS s FROM api_usage WHERE provider=? AND utc_day=?"
        args: tuple[str, ...] = (provider, day)
        if tier is not None:
            sql += " AND tier=?"
            args = (provider, day, tier)
        return float(self.conn.execute(sql, args).fetchone()["s"])

    def record(
        self, provider: str, endpoint: str, operation: str, *, units: float = 1,
        cost_usd: float = 0.0, status_code: int | None = None, run_id: int | None = None,
        detail: str | None = None, ts: str | None = None, tier: str | None = None,
        llm: LLMUsage | None = None,
    ) -> None:
        when = ts or now_iso()
        self.conn.execute(
            "INSERT INTO api_usage(ts, utc_day, provider, endpoint, operation, units, cost_usd,"
            " status_code, run_id, detail, tier, model, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, is_batch)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (when, self._day(when), provider, endpoint, operation, units, cost_usd,
             status_code, run_id, detail, tier,
             llm.model if llm else None,
             llm.input_tokens if llm else None,
             llm.output_tokens if llm else None,
             llm.cache_read_tokens if llm else None,
             llm.cache_write_tokens if llm else None,
             int(llm.is_batch) if llm else 0),
        )

    def check(
        self, provider: str, estimated_cost_usd: float, ts: str | None = None,
        *, tier: str | None = None,
    ) -> None:
        """Raise if spending ``estimated_cost_usd`` now would exceed today's cap.

        Providers without a configured budget are refused for any non-zero cost,
        so a new paid integration cannot spend money until a cap exists. For a
        provider with tier caps, a paid call must name a configured tier and must
        fit both the tier cap and the provider cap.
        """
        if estimated_cost_usd < 0:
            raise ValueError("cost cannot be negative")
        budget = self.budgets.providers.get(provider)
        if budget is None:
            if estimated_cost_usd > 0:
                raise BudgetExceededError(f"no budget configured for paid provider '{provider}'")
            return
        spent = self.spent_today(provider, ts)
        if spent + estimated_cost_usd > budget.daily_cap_usd + 1e-12:
            raise BudgetExceededError(
                f"{provider}: ${spent:.4f} spent today + ${estimated_cost_usd:.4f} would exceed"
                f" daily cap ${budget.daily_cap_usd:.2f}"
            )
        if budget.tiers and estimated_cost_usd > 0:
            if tier is None:
                raise BudgetExceededError(f"{provider}: paid call must name a model tier")
            if tier not in budget.tiers:
                raise BudgetExceededError(f"{provider}: no daily cap for tier '{tier}'")
            tier_cap = budget.tiers[tier]
            tier_spent = self.spent_today(provider, ts, tier=tier)
            if tier_spent + estimated_cost_usd > tier_cap + 1e-12:
                raise BudgetExceededError(
                    f"{provider}/{tier}: ${tier_spent:.4f} spent today + "
                    f"${estimated_cost_usd:.4f} would exceed tier cap ${tier_cap:.2f}"
                )
        if budget.daily_cap_usd > 0 and (spent + estimated_cost_usd) >= (
            budget.warn_fraction * budget.daily_cap_usd
        ):
            log.warning(
                "%s spend at %.0f%% of daily cap", provider,
                100 * (spent + estimated_cost_usd) / budget.daily_cap_usd,
            )

    def charge(
        self, provider: str, endpoint: str, operation: str, cost_usd: float, *,
        units: float = 1, status_code: int | None = None, run_id: int | None = None,
        detail: str | None = None, tier: str | None = None, llm: LLMUsage | None = None,
    ) -> None:
        """Atomically check provider + tier caps and record the spend (BEGIN IMMEDIATE)."""
        ts = now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.check(provider, cost_usd, ts, tier=tier)
            self.record(provider, endpoint, operation, units=units, cost_usd=cost_usd,
                        status_code=status_code, run_id=run_id, detail=detail, ts=ts,
                        tier=tier, llm=llm)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def status(self, provider: str) -> SpendStatus:
        ts = now_iso()
        spent = self.spent_today(provider, ts)
        lifetime = float(self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS s FROM api_usage WHERE provider=?", (provider,)
        ).fetchone()["s"])
        budget = self.budgets.providers.get(provider)
        cap = budget.daily_cap_usd if budget else None
        credit = budget.initial_credit_usd if budget else None
        return SpendStatus(
            provider=provider,
            utc_day=self._day(ts),
            spent_usd=spent,
            cap_usd=cap,
            remaining_today_usd=None if cap is None else max(cap - spent, 0.0),
            lifetime_spent_usd=lifetime,
            credit_remaining_usd=None if credit is None else credit - lifetime,
        )
