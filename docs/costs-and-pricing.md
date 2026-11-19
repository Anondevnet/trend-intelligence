# Costs, pricing assumptions and caps

Unit prices are kept in `config/pricing.yaml`, each block with its source URL and retrieval date. Re-verify them before relying on them.

## Daily caps (enforced in code)

`config/budgets.yaml` sets the caps. `BudgetGuard.charge()` checks the cap and records the spend in one locked transaction, and it refuses any call that would exceed the cap.
A paid provider with no configured budget is refused for any non-zero cost.
The day is the **UTC** day, the same window X uses for billing and deduplication.

| Provider | Daily cap | Notes |
|---|---|---|
| X | **$0.15** | A post costs $0.015; a post containing a URL costs **$0.20**, more than the whole daily cap. Link posts therefore need your approval for each post and a raised cap that day |
| Anthropic | **$2.00** | Opus 5.5 for research and drafting; Haiku 5.5 for clustering and quality checks |
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

## Anthropic (cached reference 2026-11-03; verify on the Claude pricing page)

| Model | Input $/MTok | Output $/MTok |
|---|---|---|
| claude-opus-5-5 | 4.00 | 20.00 |
| claude-haiku-5-5 | 0.10 | 0.50 |

The Batch API costs 50% less. Cached prompt reads cost $0.20/MTok on Opus.

## Stages 1–3 spend

$0. No paid API is called yet.
