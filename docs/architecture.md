# Architecture

## Data flow

```
launchd (every 15 min) → liquidmexx collect --due
   └─ for each enabled source whose interval has elapsed:
        adapter.collect()  ── PoliteClient (HTTPS only, pacing, retries, usage ledger)
        persist()          ── one transaction per run: raw_items + observations
   └─ link_new_items()     ── taxonomy aliases + URL entities → trend_mentions
```

## Storage model (SQLite, WAL)

| Table | Mutability | Purpose |
|---|---|---|
| `sources`, `collection_runs` | runs are updated when they finish | Provenance of every collection run |
| `raw_items` | **append-only** (trigger) | First-seen record of each external item |
| `raw_payloads` | can be pruned | Gzipped source JSON, kept separate so it can be dropped to save disk |
| `observations` | **append-only** (trigger) | Time series: `(subject, metric, value, observed_at, available_at, is_backfill)` |
| `api_usage` | **append-only** (trigger) | Every request and its cost, by UTC day; LLM calls also record tier, model, tokens and batch flag (0004) |
| `trends`, `trend_aliases` | additive | Narratives from `config/taxonomy.yaml`, plus entity trends |
| `trend_mentions` | **append-only** (trigger) | item → trend links, versioned by `linker_version` |
| `scoring_runs`, `trend_scores` | **append-only** (trigger) | Stage 4 score runs with config snapshot and hash (0005) |
| `backtest_runs`, `backtest_points` | **append-only** (trigger) | Stage 5/6 backtest and comparison runs (0006) |
| `post_drafts`, `post_events` | **append-only** (trigger) | Stage 10 immutable drafts and their audit log; approval and publish rules are enforced by triggers (0007) |
| `llm_batches`, `llm_batch_results` | **append-only** (trigger) | Stage 8 Batch API submissions and results, each result collected once (0008) |
| `schema_migrations` | additive | Applied migrations with checksums |

Views (derived, never destructive):
- `v_raw_items.is_initial_snapshot` marks each source's first live run as a catalog baseline, not new activity.
- `v_repo_quality.suspected_clone_farm` flags the same repo name published by 3 or more owners (spam or malware lures).

## Point-in-time correctness (no leakage)

- `observed_at` is the moment a value refers to. `available_at` is the earliest moment we could have known it. `available_at >= observed_at` is enforced by a CHECK constraint.
- Live data: `available_at` is when we fetched it. A live row can never claim a future time.
- Backfill: only timestamps that were genuinely public at the time are used (HN `created_at`, GitHub `starred_at`). Undated items are dropped from backfill.
- All historical reads (`Store.value_as_of`, `series_as_of`, `items_available_as_of`) filter on `available_at <= as_of`. Tests insert later and late-arriving data and assert that the earlier reads don't change.
- Timestamps are fixed-width UTC strings, so string order equals time order.

## Migrations

`migrations/NNNN_name.sql` files are applied in order, each inside a transaction.
An applied migration that has since been edited is refused (checksum mismatch), so add a new migration instead.
When the database already holds data, a backup is written to `data/backups/` before any migration runs.

## Trend identity (Stage 3, linker v1)

- **Narratives:** phrase aliases (case-insensitive, word-bounded, space or hyphen tolerant) and exact aliases (case-sensitive).
  Optional `requires_context` words help disambiguate (e.g. `MCP` must appear with an AI term).
- **Entities:** a discussion item (HN, Reddit or RSS) that links a GitHub repo or HF model creates `entity/<type>/<key>`. That link is cross-platform confirmation.
- Mentions take the item's `available_at`. `trends.first_detected_at` is the earliest evidence, and a trigger only lets it move earlier.

## Trend scoring (Stage 4, scorer s2)

`liquidmexx score [--as-of ISO] [--no-save] [--verify RUN_ID]` is deterministic. It uses no LLM and no network, and reads only rows with `available_at <= as_of`.

| Sub-score | Weight | How it's computed |
|---|---|---|
| acceleration | 0.40 | z-score of the last 24h **per source**, measured against that source's own 14-day rolling baseline. It's computed only when the source has items in at least 7 of the 14 baseline buckets, and fewer than 3 mentions can't produce a spike. Sources are combined by credibility |
| volume | 0.25 | Mentions in the window, weighted by credibility and quality |
| breadth | 0.20 | Distinct sources in the window; 4 or more counts as full cross-platform confirmation |
| novelty | 0.15 | Age of the first eligible mention, computed only if that source was already collecting before it. Scaled by window support |

- **Score:** 100 × Σ weight × sub-score. A sub-score that can't be computed counts as 0 and is never renormalised up. `confidence` is the share of weight that was actually computable.
- **Baseline items:** first-run catalogs (DeFiLlama, MCP Registry, RSS back-issues, GitHub's 14-day window, ...) are excluded. HN's first run was a 24h lookback of new stories that the backfill skipped, so it is counted (`first_run_counts_for`).
- **HN comparability:** the backfill only kept stories that reached 3 or more points. A live story counts only once 3+ points are on record by `as_of`. This is conservative and never over-counts.
- **Clone farms:** a GitHub repo name owned by 3 or more owners, counting only repos known by `as_of`. Those mentions count at weight 0.1, including entity trends for such repos.
- **First detection:** computed from mentions available by `as_of`, never from `trends.first_detected_at`, which can move earlier when backfill arrives.
- **Reproducibility:** each run stores its full config snapshot and hash in `scoring_runs`. `trend_scores` is append-only. `--verify` recomputes a run from its own snapshot and compares exactly.
- **Rules:** bump `SCORER_VERSION` for any logic change, and bump the linker version when the taxonomy changes. Otherwise new aliases would add mentions to past dates.

## Backtesting (Stage 5)

`liquidmexx backtest [--horizon N] [--no-save]` evaluates detectors at daily times T (12:00 UTC) for horizons of 1, 7 and 30 days.

| Guard | How |
|---|---|
| Ranking uses only the past | The detector sees only rows with `available_at <= T` |
| The outcome uses only the outcome window | Mentions in `(T, T+H]`, read as of `T+H`, with the same eligibility rules as scoring |
| No fake emergence when a source starts | The label counts only sources that were active on ≥ 7 of the 14 days before T **and** on ≥ 70% of the outcome days |
| No future label in a decision | Walk-forward trains only on dates with `t + H <= T_test` (embargo) and uses an expanding window. Thresholds are training-score quantiles |
| No invented history | Evaluation dates need ≥ 8 days of history before T and a complete outcome window after it. Otherwise the result is `insufficient_history` |

- **Label:** a trend counts as emerged when its future daily rate is at least 2× its own 14-day baseline **and** it has ≥ 3 weighted mentions in the outcome window.
- **Universe:** trends with an eligible mention in the 14 days before T. Trends that first appear after T are reported as `unseen_emergers`.
- **Detectors:** `scorer:<version>`, `volume_only` and a seeded `random`. Each is a function from `as_of` to `{trend: score}`, ready for Stage 6.
- **Metrics:** precision@5 and @10 with raw hit counts, base rate, lift, Spearman ρ against the future log-lift, walk-forward out-of-sample precision/recall/F1, and the backfill share.
- **Storage:** runs are stored append-only in `backtest_runs` and `backtest_points`, with the config snapshot and the data watermark.

**Known bias:** the HN backfill kept only stories that eventually reached 3+ points, which is hindsight. Results with `backfill_share` near 1.0 are therefore optimistic.

### Current result (2026-11-19): the scorer loses to the volume baseline

Measured on Hacker News only, with every input from backfill. The scorer is **not** tuned to fit these numbers.

| Horizon | `scorer:s2` top-5 hits | `volume_only` top-5 hits | `random` |
|---|---|---|---|
| 1 day (21 dates) | 4 / 105 | **9 / 105** | 0 / 105 |
| 7 days (15 dates) | 1 / 75 | **5 / 75** | 0 / 75 |
| 30 days | insufficient history | insufficient history | insufficient history |

The scorer finds trends that are busy *now*, and those spikes usually revert. Plain mention volume predicts next-day and next-week emergence better on this data.
The sample is small (16 positives at 1 day, 28 at 7 days) and relies on the biased backfill, so neither detector's numbers are reliable yet.
Re-run `liquidmexx backtest` once live collection has 7 or more days of data from every source. Until a detector beats `volume_only` out of sample, scores are descriptive only and must not drive posting.

## Detector comparison (Stage 6)

`config/detectors.yaml` defines two more detectors and an ensemble. Each is a function `T -> {trend: score}` over the same point-in-time inputs as the scorer: eligible mentions in the 24h window ending at T, and engagement values known at T. They are evaluated by the Stage 5 backtester against the same labels.

| Detector | Idea (reimplemented, nothing copied) |
|---|---|
| `hotlist` | From TrendRadar (GPL-3.0). Items are ranked by engagement within their source: HN points, GitHub stars, HF likes, Polymarket 24h volume. The score is 0.4 × prominence (how high the trend's items rank, with ranks capped at 10), 0.4 × frequency (weighted mentions, capped at 10) and 0.2 × hotness (share of items in the source's top 3). |
| `signal_sum` | From solana-narrative-detector (no licence). Each mention adds fixed points: GitHub ≥50★ 2, ≥10★ 1, else 0.5; HN ≥100 points 2, else 1; RSS 1.5; other sources 1. The sum is multiplied by a narrative weight (ai-agents 1.2, depin 1.1). |
| `ensemble` | Weighted mean of the members' percentile ranks (scorer, volume_only, hotlist, signal_sum). A member's weight at T is its mean top-10 precision above the base rate, computed only on earlier dates whose outcome windows had closed by T (`t + H <= T`). A member with no edge gets weight 0. With fewer than 5 such dates, or if no member has an edge, all members get equal weight, and the run records which case applied. |

The settings were fixed before any comparison was run. They must not be tuned to the backtest.

**Pre-registration lock.** `config/detectors.lock` holds the SHA-256 of `config/detectors.yaml`. It was committed before the first comparison run, and `liquidmexx compare` refuses to run if the file no longer matches.

- Detector settings are not changed after seeing results.
- Every comparison outcome is reported as it comes out, including the case where nothing beats `volume_only`.
- A deliberate change needs a new lock committed *before* the next run, with its reason recorded.

`liquidmexx compare` runs every detector and gives each one a verdict against `volume_only`:

- **INSUFFICIENT DATA** if there are fewer than 7 evaluation dates, fewer than 20 positive labels, the backfill share is above 0.5, or there is no usable history.
- **BEATS BASELINE** only if the detector has more top-10 hits than the baseline on more dates than it has fewer (one-sided sign test, p ≤ 0.10) **and** its walk-forward F1 is at least the baseline's.
- **NOT BETTER THAN BASELINE** otherwise.

The ensemble is never ranked first by default. It must earn that verdict like any other detector.

Each verdict comes with recall@10, false-positive rate@10 and walk-forward F1. Lead time and per-category breakdowns are left for the dashboard stage.

`compare` refuses to run, and saves nothing, until at least 2 sources have 7 or more days of live (non-backfill) collection. Live collection started on 2026-11-19, so the first comparison is due around 2026-11-26. No comparison has been run yet.

## Crypto and AI intelligence (Stage 7)

`liquidmexx intel crypto|ai [--as-of ISO] [--json]` prints a report rebuilt from stored history.
It uses no network and no LLM, saves nothing, and reads only rows with `available_at <= as_of`, so
a past report can be rebuilt exactly (a test checks that later data doesn't change it). Settings
are in `config/intel.yaml`.

| Section | Crypto report | AI report |
|---|---|---|
| Narrative activity | solana, defi, depin, crypto-ai, crypto-infra, prediction-markets, trading | ai-agents, mcp, local-ai, open-models, ai-research, crypto-ai |
| Measured movers | Protocol and chain TVL (DeFiLlama), Solana non-vote TPS | Hugging Face model likes |
| New listings | New on DeFiLlama (a listing is not necessarily a launch) | New MCP registry servers, new arXiv papers |
| Other | Polymarket crypto markets by 24h volume | |
| Both | GitHub repos (age, stars, star and fork growth, stars per day, clone-farm flag) and RSS headlines | |

- **Narratives** use the scorer's eligible mentions (first-run catalogs excluded, clone farms
  discounted) in the last 24h. They are ordered by weighted mentions, because volume is the only
  ranking not beaten so far. The latest stored score is shown next to it. Neither is a prediction.
- **Concentration.** When one source supplies 80% or more of a narrative's activity, the report
  says so. On 2026-11-20, for example, 99% of `agent-payments` came from one publisher's bulk
  MCP registry listings.
- **Movers** compare the freshest reading at T (no older than 6h) with the reading that was
  *known* at T − 24h. Without such a reading the change is shown as unknown, never guessed.
- **Evidence labels.** Every evidence row carries an evidence class and a `claim_status` in the
  drafts' vocabulary:

  | Class | Sources | `claim_status` |
  |---|---|---|
  | measured | DeFiLlama, GitHub, Hugging Face, Solana RPC, MCP registry | verified: only that the API returned this value or item at that time |
  | market_odds | Polymarket | speculative: bets are expectations, not facts |
  | reported | RSS, arXiv | speculative: the publication exists, its claims are not verified here |
  | discussion | Hacker News, Reddit | speculative |
  | rumor | reported or discussion items with rumor wording ("reportedly", "leaked", "sources say", ...) | rumor |

- **GitHub stars are attention, not quality.** Each report lists what is not collected: commit
  frequency, contributor growth, release activity, documentation quality and practical
  usefulness. Brand-new repos with fast stars are often promotion or spam.
- **No recommendations.** Reports describe activity and measured changes. They never say what to
  buy or sell, and they never feed posting directly.

## Dashboard (Stage 9)

`liquidmexx dashboard [--port 8741]` serves a local, read-only page at `http://127.0.0.1:8741/`.
It uses only the Python standard library (no new dependencies) and renders HTML on the server,
with no JavaScript.

| Page | Shows |
|---|---|
| `/` | Live days per source toward the Stage 6 comparison, lock status and readiness; top trends (latest stored scores plus 24h mentions by source); posting mode, kill switch and draft queue; spend today against daily and per-tier caps; last collection run per source |
| `/intel/crypto`, `/intel/ai` | The Stage 7 reports |

- **Every number comes from stored data.** Each page load reads `data/liquidmexx.db` and the
  config files. Nothing is cached, sampled or illustrative.
- **Concentration flag.** Any topic where one source supplies 80% or more of its last-24h
  activity is flagged, for example `agent-payments`, which is nearly all MCP registry listings.
  With very few mentions the flag is trivially true (one mention is 100% of one source).
- **Read-only database.** Each request opens the database with `mode=ro` and
  `PRAGMA query_only = ON`, never migrates, and closes it again. A missing database gives an
  error page; it is never created.
- **Local only.** The server binds to 127.0.0.1, and there is no option to change that. Requests
  whose `Host` header is not `127.0.0.1:<port>` or `localhost:<port>` are refused (421), which
  blocks DNS-rebinding.
- **Nothing to click that changes anything.** Only GET and HEAD are answered; every other method
  gets 405. Pages have no forms, buttons or scripts, and the content-security policy forbids
  them (`default-src 'none'; form-action 'none'`). Approving and publishing stay in the terminal.
- **Safe rendering.** All collected text is HTML-escaped. Links are kept only for http(s) URLs
  and open with `noopener noreferrer`.
- Times are shown in America/Detroit, with the UTC value in a tooltip.

## Stage status

| Stage | What it adds | Status |
|---|---|---|
| 1–3 | Collection, append-only storage, trend linking | Done |
| 4 | Deterministic scorer | Done |
| 5 | Point-in-time backtester | Done (scorer loses to `volume_only` so far) |
| 6 | Detector comparison | Code done and settings locked; first run due 2026-11-26, see [`comparison-checklist.md`](comparison-checklist.md) |
| 7 | Crypto and AI intelligence | Done: `liquidmexx intel crypto` and `intel ai`, descriptive reports with labelled evidence |
| 8 | Claude research and content | Offline client done (routing, caps, validation, caching, batches); real calls off, see [`costs-and-pricing.md`](costs-and-pricing.md) |
| 9 | Dashboard | Done: `liquidmexx dashboard`, local (127.0.0.1) and read-only |
| 10 | X publishing | Safeguards done (off, draft-only, approval-gated); no live X transport exists, see [`publishing-safety.md`](publishing-safety.md) |
| 11 | Engagement analytics | Planned |
| 12 | Learning engine | Planned |
| 13 | Security and operations hardening | Planned |
