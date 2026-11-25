# Trend Intelligence

[![CI](https://github.com/Anondevnet/trend-intelligence/actions/workflows/ci.yml/badge.svg)](https://github.com/Anondevnet/trend-intelligence/actions/workflows/ci.yml)
[![Python 3.11](https://img.shields.io/badge/python-3.11-blue)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A local-first pipeline that collects crypto and AI signals into an append-only history, scores which topics are accelerating, and backtests those calls point-in-time so the scores can't see the future, with LLM drafting and posting kept behind approval gates that are off by default.

![Demo](docs/demo.gif)

## Quickstart

Uses [uv](https://docs.astral.sh/uv/) and Python 3.11. Tests mock all HTTP and the Claude client, so no keys are needed.

```bash
git clone https://github.com/Anondevnet/trend-intelligence && cd trend-intelligence
uv sync                          # .venv with pinned deps, including the dev group
uv run pytest                    # 231 offline tests
uv run trend-intel migrate       # create the local SQLite store in data/
uv run trend-intel score         # deterministic trend scores from stored history (no network)
```

Sources, collection, and scheduling are in [Install](#install) and [Operate](#operate).

## Overview

A local-first crypto × AI trend intelligence platform.
It collects signals from public sources and stores them in a history that is never edited.
It then scores which topics are *accelerating* rather than merely popular, and backtests those predictions honestly.
Later stages draft original, sourced content for review and posting.

> **Status:** Stages 1–5, 7 and 9 are done (collection, scoring, backtesting, crypto and AI intelligence reports, a local read-only dashboard). The Stage 6 detector code is written; its
> comparison waits for 7+ days of live data (earliest 2026-11-26; settings locked in `config/detectors.lock`). Also done: the Stage 10 posting
> safeguards: an approval queue, quality gate, kill switch and audit log, all off and draft-only,
> and the Stage 8 Claude client (routing, caps, validation with one Sonnet retry, caching, batches),
> with real API calls off.
> Nothing posts to X unless posting mode is `live`, the opt-in env value and X write keys are set, and you approved the draft. See [`docs/publishing-safety.md`](docs/publishing-safety.md).

## Principles

- **The history can't be edited.** Observations and raw items are protected by database triggers against UPDATE and DELETE.
- **Point-in-time reads.** Every observation stores when it was observed (`observed_at`) and when it could first have been known (`available_at`).
  Historical queries filter on `available_at <= as_of`, so backtests can't see the future.
- **Backfilled data is labelled.** It is flagged `is_backfill = 1` and dated only with timestamps that were genuinely knowable at the time.
- **Costs are capped in code.** Daily caps live in `config/budgets.yaml`. Every external request is written to `api_usage`.
- **No secrets in the repository.** Credentials come only from `.env` or the environment. Logs are redacted, and `scripts/secret_scan.py` runs before every commit.

## Requirements

- macOS on Apple Silicon (tested on a Mac mini M4), Python 3.11, [uv](https://docs.astral.sh/uv/).
- No Docker, Redis or local models.

## Install

From the repository root:

```bash
uv sync                                   # creates .venv with pinned deps
cp config/env.template .env && chmod 600 .env   # then fill in what you have (all optional)
uv run trend-intel migrate                # creates data/trend_intel.db
uv run trend-intel sources                # shows which sources are enabled / have credentials
```

## Operate

```bash
uv run trend-intel collect --due          # run every source whose interval has elapsed
uv run trend-intel collect --source github # run one source now
uv run trend-intel link                   # link new items to trends/aliases (also runs after collect)
uv run trend-intel score                  # deterministic trend scores from stored history (no network)
uv run trend-intel backtest               # point-in-time backtests (1/7/30 days) vs baselines
uv run trend-intel compare                # Stage 6 detector comparison (refuses before 7+ live days)
uv run trend-intel intel crypto           # Stage 7 report from stored data (also: intel ai, --as-of, --json)
uv run trend-intel dashboard              # read-only dashboard at http://127.0.0.1:8741/ (Ctrl+C to stop)
uv run trend-intel backfill github-stars  # honest star-history backfill from starred_at timestamps
uv run trend-intel backfill hackernews --days 30
uv run trend-intel queue list             # post drafts awaiting review (approval is interactive only)
uv run trend-intel posting status         # mode (off), kill switch, live opt-in
uv run trend-intel status                 # counts, last runs, today's spend vs caps
uv run trend-intel backup                 # consistent SQLite snapshot into data/backups/
```

Scheduled collection uses launchd. Install it yourself with the commands in [`docs/operations.md`](docs/operations.md).

## Configuration

| File | Purpose |
|---|---|
| `config/sources.yaml` | Turn each source adapter on or off; set intervals and parameters |
| `config/taxonomy.yaml` | Trend categories, narratives and aliases used for linking |
| `config/budgets.yaml` | Daily USD caps per provider (X $0.15, Anthropic $2.00) |
| `config/pricing.yaml` | Unit prices, with source URL and retrieval date |
| `config/models.yaml` | LLM model IDs, task-to-tier routing, escalation, caching/batch settings, `live_calls: false` |
| `config/scoring.yaml` | Scoring windows, exclusions, clone-farm discount, sub-score weights |
| `config/backtest.yaml` | Backtest horizons, outcome label, walk-forward settings |
| `config/detectors.yaml` | Stage 6 detectors (hotlist, signal_sum, ensemble) and comparison rules. **Locked**: `config/detectors.lock` |
| `config/intel.yaml` | Stage 7 report groups, mover windows, evidence labels and rumor wording |
| `config/publishing.yaml` | Posting mode (off), kill switch, approval TTL, daily cap, quality-gate rules |
| `config/env.template` | Template for `.env`, with placeholders only |

## Development

```bash
uv run ruff check . && uv run mypy && uv run pytest
uv run python scripts/secret_scan.py      # run before every commit
```

Tests use mocked HTTP (`respx`) and a mock Claude transport, never touch real credentials, never call Anthropic or X, and never post.


## Results

Recorded in [GitHub Actions](https://github.com/Anondevnet/trend-intelligence/actions/runs/45046516792) on 22 November 2026, `ubuntu-latest`, Python 3.11, HTTP mocked with `respx`:

| Metric | Value |
|---|---|
| Tests passed | 231 |

The job completed successfully. The runner logged the 231-test progress bar through 100%.

## Documentation

- [`docs/architecture.md`](docs/architecture.md): components, schema and data flow
- [`docs/data-sources.md`](docs/data-sources.md): every source, its terms, limits and what we store
- [`docs/operations.md`](docs/operations.md): launchd install, backups, recovery
- [`docs/costs-and-pricing.md`](docs/costs-and-pricing.md): pricing assumptions and caps
- [`docs/publishing-safety.md`](docs/publishing-safety.md): posting safeguards (off, draft-only, approval-gated)
- [`docs/comparison-checklist.md`](docs/comparison-checklist.md): the 2026-11-26 Stage 6 comparison run and what each outcome means

## License

MIT. See [LICENSE](LICENSE). No third-party detector code is vendored here.
TrendRadar (GPL-3.0) and solana-narrative-detector (no licence) were studied only for ideas, and their scoring approaches are reimplemented independently as baseline detectors.
