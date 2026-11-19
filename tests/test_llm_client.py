"""Stage 8 Claude client, offline: every test uses a scripted mock transport.

No test can reach Anthropic: the default transport refuses all calls, and the SDK client is
patched to explode if anything tries to construct it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

import pytest

from liquidmexx.config import (
    ModelsConfig,
    load_budgets,
    load_models,
    load_pricing,
)
from liquidmexx.costs.budget import BudgetExceededError, BudgetGuard
from liquidmexx.llm import (
    BatchItem,
    ClaudeClient,
    DailyTaskLimitError,
    LiveCallsDisabledError,
    LLMError,
    OfflineTransport,
    live_allowed,
    make_transport,
)
from liquidmexx.llm.pricing import Usage, cost_usd, worst_case_cost
from liquidmexx.llm.schemas import EntityExtractionOut, PostDraftOut, json_schema
from liquidmexx.settings import Settings

PRICING = load_pricing()
SYSTEM = "You extract entities from crypto and AI news. Return JSON only."


@pytest.fixture(autouse=True)
def no_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing the real SDK client anywhere in these tests is a failure."""
    import anthropic

    def boom(*_: Any, **__: Any) -> None:
        raise AssertionError("the real Anthropic client must not be constructed in tests")

    monkeypatch.setattr(anthropic, "Anthropic", boom)


def msg(text: str, *, model: str = "claude-haiku-5-5", stop: str = "end_turn",
        usage: dict[str, int] | None = None) -> dict[str, Any]:
    return {"id": "msg_mock", "type": "message", "model": model, "stop_reason": stop,
            "content": [{"type": "text", "text": text}],
            "usage": usage or {"input_tokens": 1000, "output_tokens": 200,
                               "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}


ENTITIES = json.dumps({"entities": ["Solana", "MCP"]})
BAD = '{"entities": "not a list"}'


class MockTransport:
    def __init__(self, replies: Sequence[dict[str, Any]] = (), *,
                 batch_status: str = "ended", batch_results: Sequence[dict[str, Any]] = ()
                 ) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.batches: list[list[dict[str, Any]]] = []
        self.status = batch_status
        self.results = list(batch_results)

    def create(self, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(params)
        return self.replies.pop(0)

    def create_batch(self, requests: Sequence[dict[str, Any]]) -> str:
        self.batches.append(list(requests))
        return f"msgbatch_mock_{len(self.batches)}"

    def batch_status(self, batch_id: str) -> str:
        return self.status

    def batch_results(self, batch_id: str) -> list[dict[str, Any]]:
        return self.results


def client(conn: sqlite3.Connection, transport: Any, models: ModelsConfig | None = None
           ) -> ClaudeClient:
    return ClaudeClient(conn, models or load_models(), PRICING,
                        BudgetGuard(conn, load_budgets()), transport)


def rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM api_usage WHERE provider='anthropic' ORDER BY id"
                        ).fetchall()


# ------------------------------------------------------------- offline by default


def test_shipped_config_keeps_live_calls_off() -> None:
    models = load_models()
    assert models.live_calls is False
    assert isinstance(make_transport(models, Settings(_env_file=None)),  # type: ignore[call-arg]
                      OfflineTransport)


def test_live_needs_config_env_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    on = load_models().model_copy(update={"live_calls": True})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    assert not live_allowed(on, Settings(_env_file=None))  # type: ignore[call-arg]
    monkeypatch.setenv("LIQUIDMEXX_LIVE_LLM", "yes")
    assert not live_allowed(on, Settings(_env_file=None))  # type: ignore[call-arg]
    monkeypatch.setenv("LIQUIDMEXX_LIVE_LLM", "enabled")
    assert live_allowed(on, Settings(_env_file=None))  # type: ignore[call-arg]
    assert not live_allowed(load_models(), Settings(_env_file=None))  # type: ignore[call-arg]


def test_offline_transport_refuses_and_records_nothing(conn: sqlite3.Connection) -> None:
    c = client(conn, OfflineTransport())
    with pytest.raises(LiveCallsDisabledError):
        c.run("entity_extraction", system=SYSTEM, user="text")
    with pytest.raises(LiveCallsDisabledError):
        c.submit_batch("weekly_digest", system="s", items=[BatchItem("a", "u")],
                       output=PostDraftOut)
    assert rows(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM llm_batches").fetchone()[0] == 0


# ------------------------------------------------------------------ routing


def test_routing_follows_models_yaml(conn: sqlite3.Connection) -> None:
    c = client(conn, MockTransport())
    assert c.build_params("entity_extraction", "haiku", "s", "u", None)["model"] == \
        "claude-haiku-5-5"
    assert c.route("post_draft").tier == "sonnet"
    assert c.route("claim_verification").tier == "opus"
    with pytest.raises(ValueError, match="must not call an LLM"):
        c.run("scoring", system="s", user="u")
    with pytest.raises(KeyError):
        c.run("no_such_task", system="s", user="u")


def test_request_shape_caching_schema_and_no_thinking(conn: sqlite3.Connection) -> None:
    t = MockTransport([msg(ENTITIES)])
    client(conn, t).run("entity_extraction", system=SYSTEM, user="Solana MCP server")
    p = t.calls[0]
    assert p["system"] == [{"type": "text", "text": SYSTEM,
                            "cache_control": {"type": "ephemeral"}}]
    assert p["max_tokens"] == load_models().max_tokens["haiku"]
    fmt = p["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False
    assert "thinking" not in p and "budget_tokens" not in json.dumps(p)


def test_schema_task_without_schema_is_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(LLMError, match="schema"):
        client(conn, MockTransport()).run("clustering_check", system="s", user="u")


def test_batch_routed_task_needs_batch_or_urgent(conn: sqlite3.Connection) -> None:
    c = client(conn, MockTransport([msg('{"text":"t","claim_status":"verified","claims":[]}',
                                        model="claude-sonnet-5-5")]))
    with pytest.raises(LLMError, match="Batch API"):
        c.run("weekly_digest", system="s", user="u", output=PostDraftOut)
    assert c.run("weekly_digest", system="s", user="u", output=PostDraftOut, urgent=True).ok


# -------------------------------------------------- validation and escalation


def test_valid_haiku_answer_is_logged_with_tokens_and_cost(conn: sqlite3.Connection) -> None:
    res = client(conn, MockTransport([msg(ENTITIES)])).run(
        "entity_extraction", system=SYSTEM, user="u")
    assert res.ok and isinstance(res.output, EntityExtractionOut)
    assert res.output.entities == ["Solana", "MCP"] and res.autopost_allowed
    (row,) = rows(conn)
    assert (row["tier"], row["model"], row["input_tokens"], row["output_tokens"]) == (
        "haiku", "claude-haiku-5-5", 1000, 200)
    assert row["cost_usd"] == pytest.approx((1000 * 0.10 + 200 * 0.50) / 1e6)
    assert json.loads(row["detail"])["attempt"] == 1


def test_haiku_schema_failure_retries_once_on_sonnet(conn: sqlite3.Connection) -> None:
    t = MockTransport([msg(BAD), msg(ENTITIES, model="claude-sonnet-5-5")])
    res = client(conn, t).run("entity_extraction", system=SYSTEM, user="u")
    assert res.ok and [a.tier for a in res.attempts] == ["haiku", "sonnet"]
    assert [c["model"] for c in t.calls] == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert not res.autopost_allowed            # never auto-post after a failed check
    assert [r["tier"] for r in rows(conn)] == ["haiku", "sonnet"]


def test_second_failure_is_flagged_not_retried_again(conn: sqlite3.Connection) -> None:
    t = MockTransport([msg(BAD), msg(BAD, model="claude-sonnet-5-5"), msg(ENTITIES)])
    res = client(conn, t).run("entity_extraction", system=SYSTEM, user="u")
    assert not res.ok and res.flagged and res.output is None
    assert len(t.calls) == 2 and "flag_for_review" in (res.flag_reason or "")
    assert not res.autopost_allowed


def test_sonnet_task_failure_is_flagged_without_escalation(conn: sqlite3.Connection) -> None:
    t = MockTransport([msg("not json", model="claude-sonnet-5-5")])
    res = client(conn, t).run("post_draft", system="s", user="u")
    assert res.flagged and len(t.calls) == 1


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_refusal_or_truncation_counts_as_failure(conn: sqlite3.Connection, stop: str) -> None:
    t = MockTransport([msg(ENTITIES, stop=stop), msg(ENTITIES, model="claude-sonnet-5-5")])
    res = client(conn, t).run("entity_extraction", system=SYSTEM, user="u")
    assert res.attempts[0].error == f"stop_reason={stop}" and res.ok and len(t.calls) == 2


# ------------------------------------------------------------------ caps


def test_tier_cap_refuses_before_any_request(conn: sqlite3.Connection) -> None:
    guard = BudgetGuard(conn, load_budgets())
    guard.record("anthropic", "/v1/messages", "fill", cost_usd=0.1999999, tier="haiku")
    t = MockTransport([msg(ENTITIES)])
    with pytest.raises(BudgetExceededError, match="tier cap"):
        client(conn, t).run("entity_extraction", system=SYSTEM, user="u")
    assert t.calls == [] and len(rows(conn)) == 1


def test_retry_blocked_by_budget_is_flagged(conn: sqlite3.Connection) -> None:
    guard = BudgetGuard(conn, load_budgets())
    guard.record("anthropic", "/v1/messages", "fill", cost_usd=0.4999, tier="sonnet")
    t = MockTransport([msg(BAD)])
    res = client(conn, t).run("entity_extraction", system=SYSTEM, user="u")
    assert res.flagged and len(t.calls) == 1
    assert "retry not sent" in (res.attempts[-1].error or "")


def test_deep_research_is_limited_to_three_per_day(conn: sqlite3.Connection) -> None:
    c = client(conn, MockTransport())
    c.submit_batch("deep_research", system="s", output=PostDraftOut,
                   items=[BatchItem(f"r{i}", "u") for i in range(3)])
    with pytest.raises(DailyTaskLimitError):
        c.submit_batch("deep_research", system="s", output=PostDraftOut,
                       items=[BatchItem("r4", "u")])


# ------------------------------------------------------------------ pricing


def test_cost_covers_cache_and_batch_discount() -> None:
    u = Usage(input_tokens=1000, output_tokens=1000, cache_write_tokens=10_000,
              cache_read_tokens=100_000)
    sonnet = (1000 * 2.0 + 1000 * 10.0 + 10_000 * 2.5 + 100_000 * 0.10) / 1e6
    assert cost_usd(PRICING, "claude-sonnet-5-5", u) == pytest.approx(sonnet)
    assert cost_usd(PRICING, "claude-sonnet-5-5", u, batch=True) == pytest.approx(sonnet / 2)
    one_hour = (1000 * 2.0 + 1000 * 10.0 + 10_000 * 4.0 + 100_000 * 0.10) / 1e6
    assert cost_usd(PRICING, "claude-sonnet-5-5", u, cache_ttl="1h") == pytest.approx(one_hour)


def test_haiku_long_prompt_rate_applies_above_threshold() -> None:
    short = cost_usd(PRICING, "claude-haiku-5-5", Usage(input_tokens=100_000))
    long = cost_usd(PRICING, "claude-haiku-5-5", Usage(input_tokens=100_001))
    assert short == pytest.approx(0.01) and long == pytest.approx(100_001 * 0.50 / 1e6)


def test_worst_case_estimate_bounds_actual_cost() -> None:
    est = worst_case_cost(PRICING, "claude-haiku-5-5", 3000, 2048)
    actual = cost_usd(PRICING, "claude-haiku-5-5", Usage(input_tokens=1000,
                                                          output_tokens=2048))
    assert est >= actual


def test_schema_objects_are_closed() -> None:
    s = json_schema(PostDraftOut)
    assert s["additionalProperties"] is False
    assert all(d["additionalProperties"] is False for d in s["$defs"].values())


# ------------------------------------------------------------------ batches


def batch_msg(text: str) -> dict[str, Any]:
    return msg(text, model="claude-sonnet-5-5",
               usage={"input_tokens": 2000, "output_tokens": 500,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})


DIGEST = json.dumps({"text": "Weekly: agent frameworks led GitHub growth.",
                     "claim_status": "verified", "claims": [{"text": "x", "source_refs": [1]}]})


def test_batch_submit_and_collect_once(conn: sqlite3.Connection) -> None:
    results = [
        {"custom_id": "a", "result": {"type": "succeeded", "message": batch_msg(DIGEST)}},
        {"custom_id": "b", "result": {"type": "succeeded", "message": batch_msg("nope")}},
        {"custom_id": "c", "result": {"type": "errored", "error": {"type": "api_error"}}},
        {"custom_id": "zz", "result": {"type": "succeeded", "message": batch_msg(DIGEST)}},
    ]
    t = MockTransport(batch_status="in_progress", batch_results=results)
    c = client(conn, t)
    bid = c.submit_batch("weekly_digest", system="digest rubric", output=PostDraftOut,
                         items=[BatchItem(x, f"week {x}") for x in "abcd"])
    sent = t.batches[0]
    assert [r["custom_id"] for r in sent] == ["a", "b", "c", "d"]
    assert sent[0]["params"]["model"] == "claude-sonnet-5-5"
    assert sent[0]["params"]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert c.collect_batch(bid, PostDraftOut) is None          # still processing
    t.status = "ended"
    got = {r.custom_id: r for r in c.collect_batch(bid, PostDraftOut) or []}
    assert {k: v.status for k, v in got.items()} == {
        "a": "ok", "b": "invalid", "c": "errored", "d": "expired"}   # zz is not ours
    assert isinstance(got["a"].output, PostDraftOut)
    usage = rows(conn)
    assert len(usage) == 2 and all(r["is_batch"] == 1 for r in usage)
    assert usage[0]["cost_usd"] == pytest.approx((2000 * 2.0 + 500 * 10.0) / 1e6 / 2)
    with pytest.raises(LLMError, match="already collected"):
        c.collect_batch(bid, PostDraftOut)
    flagged = conn.execute("SELECT COUNT(*) FROM llm_batch_results WHERE flag_reason LIKE"
                           " 'flag_for_review%'").fetchone()[0]
    assert flagged == 3


def test_batch_rules(conn: sqlite3.Connection) -> None:
    c = client(conn, MockTransport())
    with pytest.raises(LLMError, match="not routed to the Batch API"):
        c.submit_batch("entity_extraction", system="s", items=[BatchItem("a", "u")])
    with pytest.raises(LLMError, match="unique"):
        c.submit_batch("weekly_digest", system="s", output=PostDraftOut,
                       items=[BatchItem("a", "u"), BatchItem("a", "v")])
    c.submit_batch("weekly_digest", system="s", output=PostDraftOut, items=[BatchItem("a", "u")])
    for sql in ("DELETE FROM llm_batches", "UPDATE llm_batches SET n_requests = 9"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(sql)
