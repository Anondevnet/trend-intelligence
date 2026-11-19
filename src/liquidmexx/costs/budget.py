"""Append-only cost ledger and hard daily spending caps.

Every paid call must go through ``BudgetGuard.charge`` (check-then-record in a
single transaction). Free sources call ``record`` so request volume is visible.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from liquidmexx.config import BudgetsConfig
from liquidmexx.timeutil import now_iso

log = logging.getLogger(__name__)


class BudgetExceededError(RuntimeError):
    """Raised before a call that would push today's spend over the cap."""


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

    def spent_today(self, provider: str, ts: str | None = None) -> float:
        day = self._day(ts or now_iso())
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS s FROM api_usage WHERE provider=? AND utc_day=?",
            (provider, day),
        ).fetchone()
        return float(row["s"])

    def record(
        self, provider: str, endpoint: str, operation: str, *, units: float = 1,
        cost_usd: float = 0.0, status_code: int | None = None, run_id: int | None = None,
        detail: str | None = None, ts: str | None = None,
    ) -> None:
        when = ts or now_iso()
        self.conn.execute(
            "INSERT INTO api_usage(ts, utc_day, provider, endpoint, operation, units, cost_usd,"
            " status_code, run_id, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (when, self._day(when), provider, endpoint, operation, units, cost_usd,
             status_code, run_id, detail),
        )

    def check(self, provider: str, estimated_cost_usd: float, ts: str | None = None) -> None:
        """Raise if spending ``estimated_cost_usd`` now would exceed today's cap.

        Providers without a configured budget are refused for any non-zero cost,
        so a new paid integration cannot spend money until a cap exists.
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
        detail: str | None = None,
    ) -> None:
        """Atomically check the cap and record the spend (BEGIN IMMEDIATE)."""
        ts = now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.check(provider, cost_usd, ts)
            self.record(provider, endpoint, operation, units=units, cost_usd=cost_usd,
                        status_code=status_code, run_id=run_id, detail=detail, ts=ts)
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
