# Costs, pricing assumptions and caps

Unit prices are kept in `config/pricing.yaml`, each block with its source URL and retrieval date. Re-verify them before relying on them.

## Daily caps (enforced in code)

`config/budgets.yaml` sets the caps. `BudgetGuard.charge()` checks the cap and records the spend in one locked transaction, and it refuses any call that would exceed the cap.
A paid provider with no configured budget is refused for any non-zero cost.
The day is the **UTC** day, the same window X uses for billing and deduplication.

| Provider | Daily cap | Notes |
|---|---|---|
| X | **$0.15** | A post costs $0.015; a post containing a URL costs **$0.20**, more than the whole daily cap. Link posts therefore need your approval for each post and a raised cap that day |
| Anthropic | **$2.00** total | Per-tier caps: Opus $1.30, Sonnet $0.50, Haiku $0.20 (`anthropic.tiers`) |
| Free sources | $0.00 | Requests are still logged in `api_usage` for rate-limit health |

## X API (pay-per-use; source: docs.x.com pricing page, read 2026-11-19)

| Operation | Price |
|---|---|
| Post read | $0.005 |
| User read | $0.010 |
| Owned reads (your own posts, mentions, followers) | $0.001 |
| Post create | $0.015 |
| Post create with URL | $0.200 |
| Likes/follows | $0.015, **never automated** by this app |

Re-reading the same resource within one UTC day isn't billed again. The read cap is 3M posts per month.

## Anthropic (source: platform.claude.com pricing page, read 2026-11-19)

| Model | Input | 5m cache write | 1h cache write | Cache read | Output ($/MTok) |
|---|---|---|---|---|---|
| claude-opus-5-5 | 4.00 | 5.00 | 8.00 | 0.20 | 20.00 |
| claude-sonnet-5-5 | 2.00 | 2.50 | 4.00 | 0.10 | 10.00 |
| claude-haiku-5-5 (prompt ≤100k) | 0.10 | 0.125 | 0.20 | 0.01 | 0.50 |
| claude-haiku-5-5 (prompt >100k) | 0.50 | 0.625 | 1.00 | 0.05 | 2.50 |

The Batch API takes 50% off input and output, and the discount stacks with caching. Opus and Sonnet have no long-context surcharge.

## Cost ledger

Every call is a row in `api_usage` (append-only). Migration 0004 added `tier`, `model`, the input, output, cache-read and cache-write token counts, and `is_batch`.
A paid Anthropic call must name a tier. `BudgetGuard.charge()` checks the tier cap **and** the provider cap, then records the call, all inside one `BEGIN IMMEDIATE` transaction.

## Model routing (`config/models.yaml`)

| Tier | Tasks |
|---|---|
| None (deterministic code) | scoring, z-scores, backtesting, alias dedup |
| Haiku 5.5 | entity extraction, topic tagging, clustering checks, sentiment tags, schema validation, near-duplicate checks |
| Sonnet 5.5 | post drafts, threads, weekly digest, reply drafts, engagement summaries |
| Opus 5.5 | deep research (top 3 a day), claim verification, verified/speculative/rumor classification, the final safety gate, the weekly strategy review |

If a Haiku output fails schema validation, it is retried once on Sonnet and then flagged for review. Anything that failed a check is never auto-posted; the loader refuses a config that turns this off.
Non-urgent work (deep research, the digest, engagement summaries, the strategy review) goes through the Batch API, and stable system prompts are cached.

## Claude client (Stage 8, `liquidmexx.llm`)

`ClaudeClient` is the only path to the Anthropic API. It uses the official `anthropic` SDK.

**Real calls are off.** They need all three of:
- `live_calls: true` in `models.yaml`;
- `LIQUIDMEXX_LIVE_LLM=enabled` in `.env`;
- an `ANTHROPIC_API_KEY`.

Otherwise the transport is `OfflineTransport`, which refuses every call before any network use. All tests use a scripted mock transport.

**What every request goes through:**
- **Routing.** The model comes from task → tier → model in `models.yaml`. Tasks listed under `no_llm` raise an error.
- **Caps before the call.** A worst-case cost is checked against the provider and tier caps (`budgets.yaml`) before anything is sent. The estimate assumes about 3 characters per token, bills the whole prompt at the dearer of the input and cache-write rates, and assumes all of `max_tokens` is used. If it doesn't fit, nothing is sent or recorded.
- **Cost after the call.** The actual cost comes from the response's token usage and `pricing.yaml`. That covers cache writes (5m or 1h), cache reads, Haiku's rate above 100k tokens, and the 50% batch discount. Each call is one `api_usage` row with tier, model, tokens and `{"attempt": n, "ok": ...}`.
- **Validation.**
  - Structured tasks send `output_config.format` (`json_schema` built from pydantic models, with every object closed) and validate the reply against the same model.
  - A refusal or `max_tokens` stop also counts as a failure.
  - A Haiku failure is retried once on Sonnet. A second failure, or a retry the budget won't allow, is flagged `flag_for_review`.
  - `autopost_allowed` is true only for a valid first-try result. Publishing still needs human approval in every case.
- **Caching.** Tasks with `cache_system_prompt` send the system prompt as a block with `cache_control: {type: ephemeral}`, using the TTL from `caching.ttl`.
- **Batch API.**
  - Tasks with `batch: true` must use `submit_batch`. `run(..., urgent=True)` is the only way to call one synchronously.
  - Submitting checks the discounted worst case against the caps and records the batch in `llm_batches`.
  - `collect_batch` returns nothing while the batch is processing. When it has ended, it validates each result, records its discounted cost, and writes each outcome exactly once to `llm_batch_results`: ok, invalid, errored, canceled or expired, with failures flagged for review. Both tables are append-only.
  - Cost is checked when a batch is submitted but recorded when it is collected, so a collection can push that day's recorded spend past the estimate.
- **Daily task limits.** `max_per_day` (for example, deep research at 3) counts first attempts plus requests in batches not yet collected.

**Not yet verified against the live API:**
- whether the API accepts the JSON schemas exactly as pydantic generates them (`title` and `description` keys included);
- whether the SDK wrapper behaves as expected end to end.

The first real call, made only with your approval, should be a single Haiku request to check both.

## Spend so far

$0. No paid API has been called.
