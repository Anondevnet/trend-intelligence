-- 0006: Stage 5 backtests. Append-only. Each run stores its full config, the scorer
-- version/config hash it evaluated, its metrics, and every (as_of, trend) point so results
-- can be audited and compared across detectors in Stage 6.

CREATE TABLE backtest_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    detector            TEXT NOT NULL,          -- scorer:<version> | volume_only | random
    horizon_days        INTEGER NOT NULL,
    start_as_of         TEXT,
    end_as_of           TEXT,
    n_dates             INTEGER NOT NULL,
    config_hash         TEXT NOT NULL,          -- hash of backtest + scoring config snapshot
    config_json         TEXT NOT NULL,
    data_watermark      TEXT NOT NULL,          -- max raw_items.available_at used
    metrics_json        TEXT NOT NULL,
    computed_at         TEXT NOT NULL
);

CREATE TABLE backtest_points (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL REFERENCES backtest_runs(id),
    as_of               TEXT NOT NULL,
    trend_id            INTEGER NOT NULL REFERENCES trends(id),
    score               REAL NOT NULL,
    label               INTEGER NOT NULL CHECK (label IN (0, 1)),
    future_mentions     REAL NOT NULL,
    baseline_rate       REAL NOT NULL,
    backfill_share      REAL NOT NULL,          -- share of the ranking's inputs from backfill
    UNIQUE (run_id, as_of, trend_id)
);
CREATE INDEX idx_bt_points_run ON backtest_points(run_id, as_of);

CREATE TRIGGER backtest_runs_no_update BEFORE UPDATE ON backtest_runs
BEGIN SELECT RAISE(ABORT, 'backtest_runs are append-only'); END;
CREATE TRIGGER backtest_runs_no_delete BEFORE DELETE ON backtest_runs
BEGIN SELECT RAISE(ABORT, 'backtest_runs are append-only'); END;
CREATE TRIGGER backtest_points_no_update BEFORE UPDATE ON backtest_points
BEGIN SELECT RAISE(ABORT, 'backtest_points are append-only'); END;
CREATE TRIGGER backtest_points_no_delete BEFORE DELETE ON backtest_points
BEGIN SELECT RAISE(ABORT, 'backtest_points are append-only'); END;
