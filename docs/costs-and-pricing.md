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

## Stages 1–3 spend

$0. No paid API is called yet.
