-- 0005: Stage 4 trend scores. Append-only. Each scoring run snapshots its full config so a
-- stored score can be recomputed from stored history and verified bit-for-bit.

CREATE TABLE scoring_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of           TEXT NOT NULL,
    scorer_version  TEXT NOT NULL,
    linker_version  TEXT NOT NULL,
    config_hash     TEXT NOT NULL,
    config_json     TEXT NOT NULL,
    trends_scored   INTEGER NOT NULL,
    computed_at     TEXT NOT NULL,
    UNIQUE (as_of, scorer_version, config_hash)
);

CREATE TABLE trend_scores (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES scoring_runs(id),
    trend_id        INTEGER NOT NULL REFERENCES trends(id),
    as_of           TEXT NOT NULL,
    score           REAL NOT NULL,
    confidence      REAL NOT NULL,
    components      TEXT NOT NULL,      -- JSON sub-scores (null = not computable)
    evidence        TEXT NOT NULL,      -- JSON counts, baselines, exclusions, item ids
    UNIQUE (run_id, trend_id)
);
CREATE INDEX idx_scores_trend ON trend_scores(trend_id, as_of);
CREATE INDEX idx_scores_run ON trend_scores(run_id, score);

CREATE TRIGGER scoring_runs_no_update BEFORE UPDATE ON scoring_runs
BEGIN SELECT RAISE(ABORT, 'scoring_runs are append-only'); END;
CREATE TRIGGER scoring_runs_no_delete BEFORE DELETE ON scoring_runs
BEGIN SELECT RAISE(ABORT, 'scoring_runs are append-only'); END;
CREATE TRIGGER trend_scores_no_update BEFORE UPDATE ON trend_scores
BEGIN SELECT RAISE(ABORT, 'trend_scores are append-only'); END;
CREATE TRIGGER trend_scores_no_delete BEFORE DELETE ON trend_scores
BEGIN SELECT RAISE(ABORT, 'trend_scores are append-only'); END;
