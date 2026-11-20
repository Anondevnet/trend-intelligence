# Operations and recovery

## Scheduled collection (launchd, installed by you)

```bash
cd ~/trend-intelligence
zsh scripts/launchd/install.sh      # installs com.liquidmexx.collect (every 15 min) + com.liquidmexx.backup (03:30 daily)
launchctl print gui/$(id -u)/com.liquidmexx.collect | grep -E 'state|last exit'
tail -f data/logs/collect.log
```

To remove the jobs (your data is kept):

```bash
zsh scripts/launchd/uninstall.sh
```

The collector takes a file lock, so overlapping runs exit immediately. Each source runs only when its own `interval_minutes` has passed.
A failing source is recorded as `error` or `partial` and doesn't stop the others.

## Health checks

```bash
uv run liquidmexx status    # row counts, last run per source (with errors), spend vs caps, top trends
uv run liquidmexx sources   # enabled / usable / credentials present (true or false only, never values)
uv run liquidmexx score     # score trends now from stored history (no network); stores a run
uv run liquidmexx score --as-of 2026-11-12T12:00:00Z --no-save   # what we knew then
uv run liquidmexx score --verify 2                               # recompute run 2 exactly
uv run liquidmexx backtest                                       # 1/7/30-day backtests, all detectors
uv run liquidmexx intel crypto                                   # Stage 7 report (also: intel ai)
uv run liquidmexx posting status                                 # posting mode, kill switch
uv run liquidmexx posting kill --reason "..."                    # emergency stop for all publishing
```

## Backups

- `uv run liquidmexx backup` takes a consistent online snapshot using SQLite's backup API. It writes to `data/backups/` and keeps the newest 7.
- Migrations write a `pre-migrate` snapshot automatically when the database already holds data.

## Recovery

1. Stop the scheduler: `zsh scripts/launchd/uninstall.sh`.
2. Check the database: `sqlite3 data/liquidmexx.db "PRAGMA integrity_check;"`.
3. If it's corrupt, **move** the bad file aside. Don't delete it.
   Then restore the newest good snapshot:
   ```bash
   mv data/liquidmexx.db data/liquidmexx.db.corrupt-$(date +%Y%m%d%H%M)
   cp data/backups/<newest>.db data/liquidmexx.db
   uv run liquidmexx migrate
   ```
4. Reinstall the scheduler. Data collected between the snapshot and the failure is lost, and it is **not** reconstructed or invented.

## Disk

- Live collection adds roughly 5–15 MB/day at the default intervals. The 30-day HN backfill adds about 10 MB once.
- `raw_payloads` (gzipped source JSON) is the only table you can prune; the history tables are append-only by design.

## Credentials

Copy `config/env.template` to `.env` (`chmod 600 .env`) and fill in only what you use.
`.env` is git-ignored, and `scripts/secret_scan.py` fails if it ever becomes committable.
