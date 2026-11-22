# Liquidmexx Trend Intelligence

A local-first crypto × AI trend intelligence platform for the [@Liquidmexx](https://x.com/Liquidmexx) account.
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

```bash
cd ~/trend-intelligence
uv sync                                   # creates .venv with pinned deps
cp config/env.template .env && chmod 600 .env   # then fill in what you have (all optional)
uv run liquidmexx migrate                 # creates data/liquidmexx.db
uv run liquidmexx sources                 # shows which sources are enabled / have credentials
```

## Operate

```bash
uv run liquidmexx collect --due           # run every source whose interval has elapsed
uv run liquidmexx collect --source github # run one source now
uv run liquidmexx link                    # link new items to trends/aliases (also runs after collect)
uv run liquidmexx score                   # deterministic trend scores from stored history (no network)
uv run liquidmexx backtest                # point-in-time backtests (1/7/30 days) vs baselines
uv run liquidmexx compare                 # Stage 6 detector comparison (refuses before 7+ live days)
uv run liquidmexx intel crypto            # Stage 7 report from stored data (also: intel ai, --as-of, --json)
uv run liquidmexx dashboard               # read-only dashboard at http://127.0.0.1:8741/ (Ctrl+C to stop)
uv run liquidmexx backfill github-stars   # honest star-history backfill from starred_at timestamps
uv run liquidmexx backfill hackernews --days 30
uv run liquidmexx queue list              # post drafts awaiting review (approval is interactive only)
uv run liquidmexx posting status          # mode (off), kill switch, live opt-in
uv run liquidmexx status                  # counts, last runs, today's spend vs caps
uv run liquidmexx backup                  # consistent SQLite snapshot into data/backups/
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
