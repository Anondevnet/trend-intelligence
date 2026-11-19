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
| `api_usage` | **append-only** (trigger) | Every request and its cost, by UTC day |
| `trends`, `trend_aliases` | additive | Narratives from `config/taxonomy.yaml`, plus entity trends |
| `trend_mentions` | **append-only** (trigger) | item → trend links, versioned by `linker_version` |
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

## Planned later stages

| Stage | What it adds |
|---|---|
| 4 | Scoring (sub-scores with stored evidence) |
| 5 | Backtester |
| 6 | Detector comparison |
| 7 | Narrative engines |
| 8 | Claude research and content |
| 9 | Dashboard |
| 10 | X publishing (dry-run first) |
| 11 | Engagement analytics |
| 12 | Learning engine |
| 13 | Security and operations hardening |
