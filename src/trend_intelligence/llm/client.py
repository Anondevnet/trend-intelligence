"""Stage 8 Claude client: routing, caps, schema validation with one escalation, caching, batches.

Every request goes through here:
- the model comes from models.yaml (task -> tier -> model); no-LLM tasks are refused;
- a worst-case cost is checked against the provider and tier caps BEFORE the call, and the
  real cost (from the response's token usage and pricing.yaml) is recorded after it;
- structured output is validated; a failure on Haiku is retried once on Sonnet, anything
  still failing is flagged for human review and never allowed to auto-post;
- stable system prompts carry ``cache_control``; batch-routed tasks use the Batch API.
The default transport is offline and refuses every call.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from trend_intelligence.config import ModelsConfig, TaskRoute
from trend_intelligence.costs.budget import BudgetExceededError, BudgetGuard, LLMUsage
from trend_intelligence.llm.pricing import Usage, cost_usd, worst_case_cost
from trend_intelligence.llm.schemas import TASK_SCHEMAS, json_schema
from trend_intelligence.llm.transport import Transport
from trend_intelligence.timeutil import now_iso

log = logging.getLogger(__name__)

ENDPOINT = "/v1/messages"
BATCH_ENDPOINT = "/v1/messages/batches"


class LLMError(RuntimeError):
    pass


class DailyTaskLimitError(LLMError):
    pass


@dataclass(frozen=True)
class Attempt:
    tier: str
    model: str
    cost_usd: float
    error: str | None


@dataclass
class LLMResult:
    task: str
    ok: bool
    output: BaseModel | None
    text: str
    attempts: list[Attempt] = field(default_factory=list)
    flag_reason: str | None = None

    @property
    def flagged(self) -> bool:
        return not self.ok

    @property
    def cost_usd(self) -> float:
        return round(sum(a.cost_usd for a in self.attempts), 8)

    @property
    def autopost_allowed(self) -> bool:
        """Only a first-try, valid result may ever be considered for auto-posting (Stage 10
        ladder). Human approval is still required by the publishing layer regardless."""
        return self.ok and len(self.attempts) == 1


@dataclass(frozen=True)
class BatchItem:
    custom_id: str
    user: str


@dataclass(frozen=True)
class BatchItemResult:
    custom_id: str
    status: str            # ok | invalid | errored | canceled | expired
    output: BaseModel | None
    flag_reason: str | None
    cost_usd: float


def response_text(resp: dict[str, Any]) -> str:
    return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")


def validate(resp: dict[str, Any], output: type[BaseModel] | None
             ) -> tuple[BaseModel | None, str | None]:
    """(parsed output, None) on success, (None, reason) on failure."""
    stop = resp.get("stop_reason")
    if stop in ("refusal", "max_tokens"):
        return None, f"stop_reason={stop}"
    text = response_text(resp)
    if output is None:
        return None, None if text.strip() else "empty response"
    try:
        return output.model_validate_json(text), None
    except ValidationError as exc:
        return None, f"schema validation failed: {exc.error_count()} error(s)"


class ClaudeClient:
    def __init__(self, conn: sqlite3.Connection, models: ModelsConfig, pricing: dict[str, Any],
                 guard: BudgetGuard, transport: Transport) -> None:
        self.conn = conn
        self.models = models
        self.pricing = pricing
        self.guard = guard
        self.transport = transport
        self.cache_ttl = str(models.caching.get("ttl", "5m"))

    # ---- request building ------------------------------------------------------------------
    def route(self, task: str) -> TaskRoute:
        self.models.model_for(task)       # refuses no-LLM and unknown tasks
        return self.models.tasks[task]

    def output_for(self, task: str, output: type[BaseModel] | None) -> type[BaseModel] | None:
        route = self.route(task)
        out = output or TASK_SCHEMAS.get(task)
        if route.schema_ and out is None:
            raise LLMError(f"task {task!r} requires a structured-output schema")
        return out if route.schema_ else output

    def system_blocks(self, system: str, cache: bool) -> list[dict[str, Any]]:
        block: dict[str, Any] = {"type": "text", "text": system}
        if cache:
            cc: dict[str, Any] = {"type": "ephemeral"}
            if self.cache_ttl == "1h":
                cc["ttl"] = "1h"
            block["cache_control"] = cc
        return [block]

    def build_params(self, task: str, tier: str, system: str, user: str,
                     output: type[BaseModel] | None) -> dict[str, Any]:
        route = self.route(task)
        params: dict[str, Any] = {
            "model": self.models.tiers[tier],
            "max_tokens": self.max_tokens(tier),
            "system": self.system_blocks(system, route.cache_system_prompt),
            "messages": [{"role": "user", "content": user}],
        }
        if output is not None:
            params["output_config"] = {"format": {"type": "json_schema",
                                                  "schema": json_schema(output)}}
        return params

    def max_tokens(self, tier: str) -> int:
        return int(self.models.max_tokens.get(tier, 4096))

    def _estimate(self, params: dict[str, Any], *, batch: bool) -> float:
        chars = len(json.dumps(params["system"])) + len(json.dumps(params["messages"]))
        chars += len(json.dumps(params.get("output_config", {})))
        return worst_case_cost(self.pricing, params["model"], chars, int(params["max_tokens"]),
                               batch=batch, cache_ttl=self.cache_ttl)

    # ---- limits ----------------------------------------------------------------------------
    def _check_daily_task_limit(self, task: str, route: TaskRoute, now: str,
                                n_new: int = 1) -> None:
        if route.max_per_day is None:
            return
        used = self.conn.execute(
            "SELECT COUNT(*) FROM api_usage WHERE provider=? AND operation=? AND utc_day=?"
            " AND json_extract(detail, '$.attempt') = 1",
            (self.models.provider, task, now[:10])).fetchone()[0]
        pending = self.conn.execute(
            "SELECT COALESCE(SUM(n_requests),0) FROM llm_batches b WHERE task=?"
            " AND substr(submitted_at,1,10)=? AND NOT EXISTS (SELECT 1 FROM llm_batch_results r"
            " WHERE r.batch_id=b.batch_id)", (task, now[:10])).fetchone()[0]
        if used + pending + n_new > route.max_per_day:
            raise DailyTaskLimitError(
                f"{task}: daily limit {route.max_per_day} reached ({used + pending} used today)")

    # ---- synchronous calls -----------------------------------------------------------------
    def run(self, task: str, *, system: str, user: str,
            output: type[BaseModel] | None = None, urgent: bool = False) -> LLMResult:
        route = self.route(task)
        if route.batch and not urgent:
            raise LLMError(f"task {task!r} is routed to the Batch API; use submit_batch"
                           " (or urgent=True for a one-off synchronous call)")
        out = self.output_for(task, output)
        now = now_iso()
        self._check_daily_task_limit(task, route, now)
        esc = self.models.escalation
        tier = route.tier
        result = LLMResult(task, False, None, "")
        for attempt_no in range(1, esc.max_retries + 2):
            params = self.build_params(task, tier, system, user, out)
            # Refused before any request when the caps can't absorb the worst case: nothing
            # is sent and nothing is recorded. On a retry, the result is flagged instead.
            try:
                self.guard.check(self.models.provider, self._estimate(params, batch=False),
                                 tier=tier)
            except BudgetExceededError as exc:
                if attempt_no == 1:
                    raise
                result.attempts.append(Attempt(tier, params["model"], 0.0,
                                               f"retry not sent: {exc}"))
                break
            resp = self.transport.create(params)
            usage = Usage.from_api(resp.get("usage"))
            model = str(resp.get("model") or params["model"])
            cost = cost_usd(self.pricing, params["model"], usage, cache_ttl=self.cache_ttl)
            parsed, error = validate(resp, out)
            self.guard.record(
                self.models.provider, ENDPOINT, task, cost_usd=cost, tier=tier,
                llm=LLMUsage(model, usage.input_tokens, usage.output_tokens,
                             usage.cache_read_tokens, usage.cache_write_tokens, False),
                detail=json.dumps({"attempt": attempt_no, "ok": error is None,
                                   "error": error}, sort_keys=True))
            result.attempts.append(Attempt(tier, model, cost, error))
            result.text = response_text(resp)
            if error is None:
                result.ok, result.output = True, parsed
                return result
            nxt = esc.on_schema_failure.get(tier)
            if nxt is None or attempt_no > esc.max_retries:
                break
            log.info("%s: %s on %s, retrying once on %s", task, error, tier, nxt)
            tier = nxt
        result.flag_reason = (f"{esc.after_retries}: " + "; ".join(
            f"{a.tier}: {a.error}" for a in result.attempts))
        return result

    # ---- Message Batches -------------------------------------------------------------------
    def submit_batch(self, task: str, *, system: str, items: Sequence[BatchItem],
                     output: type[BaseModel] | None = None) -> str:
        route = self.route(task)
        if not route.batch:
            raise LLMError(f"task {task!r} is not routed to the Batch API (batch: false)")
        if not items:
            raise LLMError("empty batch")
        ids = [i.custom_id for i in items]
        if len(set(ids)) != len(ids):
            raise LLMError("custom_id values must be unique within a batch")
        out = self.output_for(task, output)
        now = now_iso()
        self._check_daily_task_limit(task, route, now, n_new=len(items))
        requests: list[dict[str, Any]] = [{"custom_id": i.custom_id,
                     "params": self.build_params(task, route.tier, system, i.user, out)}
                    for i in items]
        estimate = round(sum(self._estimate(r["params"], batch=True) for r in requests), 8)
        self.guard.check(self.models.provider, estimate, tier=route.tier)
        batch_id = self.transport.create_batch(requests)
        self.conn.execute(
            "INSERT INTO llm_batches(batch_id, task, tier, model, n_requests, custom_ids_json,"
            " estimated_cost_usd, submitted_at) VALUES (?,?,?,?,?,?,?,?)",
            (batch_id, task, route.tier, self.models.tiers[route.tier], len(items),
             json.dumps(ids), estimate, now))
        return batch_id

    def collect_batch(self, batch_id: str, output: type[BaseModel] | None = None
                      ) -> list[BatchItemResult] | None:
        """None while the batch is still processing. Collects each batch exactly once."""
        row = self.conn.execute("SELECT * FROM llm_batches WHERE batch_id=?",
                                (batch_id,)).fetchone()
        if row is None:
            raise LLMError(f"unknown batch {batch_id}")
        if self.conn.execute("SELECT 1 FROM llm_batch_results WHERE batch_id=? LIMIT 1",
                             (batch_id,)).fetchone():
            raise LLMError(f"batch {batch_id} was already collected")
        if self.transport.batch_status(batch_id) != "ended":
            return None
        task, tier, model = row["task"], row["tier"], row["model"]
        out = self.output_for(task, output)
        expected = set(json.loads(row["custom_ids_json"]))
        results: list[BatchItemResult] = []
        now = now_iso()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            seen: set[str] = set()
            for item in self.transport.batch_results(batch_id):
                cid = str(item.get("custom_id"))
                if cid not in expected or cid in seen:
                    continue
                seen.add(cid)
                res = item.get("result") or {}
                kind = res.get("type")
                parsed: BaseModel | None = None
                reason: str | None = None
                cost = 0.0
                if kind == "succeeded":
                    msg = res.get("message") or {}
                    usage = Usage.from_api(msg.get("usage"))
                    cost = cost_usd(self.pricing, model, usage, batch=True,
                                    cache_ttl=self.cache_ttl)
                    parsed, reason = validate(msg, out)
                    status = "ok" if reason is None else "invalid"
                    self.guard.record(
                        self.models.provider, BATCH_ENDPOINT, task, cost_usd=cost, tier=tier,
                        llm=LLMUsage(str(msg.get("model") or model), usage.input_tokens,
                                     usage.output_tokens, usage.cache_read_tokens,
                                     usage.cache_write_tokens, True),
                        detail=json.dumps({"attempt": 1, "batch_id": batch_id,
                                           "custom_id": cid, "ok": reason is None,
                                           "error": reason}, sort_keys=True))
                else:
                    status = kind if kind in ("errored", "canceled", "expired") else "errored"
                    reason = f"batch result {kind}"
                results.append(BatchItemResult(cid, status, parsed, reason, cost))
            for cid in sorted(expected - seen):
                results.append(BatchItemResult(cid, "expired", None, "missing from results", 0.0))
            for r in results:
                self.conn.execute(
                    "INSERT INTO llm_batch_results(batch_id, custom_id, status, output_json,"
                    " flag_reason, cost_usd, collected_at) VALUES (?,?,?,?,?,?,?)",
                    (batch_id, r.custom_id, r.status,
                     r.output.model_dump_json() if r.output is not None else None,
                     None if r.status == "ok" else f"flag_for_review: {r.flag_reason}",
                     r.cost_usd, now))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return results
