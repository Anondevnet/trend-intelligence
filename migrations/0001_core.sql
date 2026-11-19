-- 0001_core: sources, collection runs, raw items, observations (append-only), cost ledger.
-- Timestamps are fixed-width ISO-8601 UTC strings (lexicographic == chronological).

CREATE TABLE sources (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE collection_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id           TEXT NOT NULL REFERENCES sources(id),
    mode                TEXT NOT NULL DEFAULT 'live',      -- live | backfill
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL DEFAULT 'running',   -- running | ok | partial | error | skipped
    requests_made       INTEGER NOT NULL DEFAULT 0,
    items_seen          INTEGER NOT NULL DEFAULT 0,
    items_new           INTEGER NOT NULL DEFAULT 0,
    observations_written INTEGER NOT NULL DEFAULT 0,
    error               TEXT
);
CREATE INDEX idx_runs_source_started ON collection_runs(source_id, started_at);

-- One row per distinct external item (story, repo, paper, post, protocol, market...).
-- First-seen record; never updated. Metrics that change live in observations.
CREATE TABLE raw_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       TEXT NOT NULL REFERENCES sources(id),
    item_type       TEXT NOT NULL,          -- story | repo | paper | model | space | mcp_server | protocol | chain | market | article | post
    external_id     TEXT NOT NULL,
    url             TEXT,
    title           TEXT,
    body            TEXT,
    author          TEXT,
    published_at    TEXT,                   -- publisher's timestamp, if any
    first_seen_at   TEXT NOT NULL,          -- when WE first saw it (live) ...
    available_at    TEXT NOT NULL,          -- ... or the earliest knowable time (backfill)
    is_backfill     INTEGER NOT NULL DEFAULT 0,
    content_hash    TEXT NOT NULL,
    run_id          INTEGER REFERENCES collection_runs(id),
    UNIQUE (source_id, item_type, external_id)
);
CREATE INDEX idx_raw_items_available ON raw_items(available_at);
CREATE INDEX idx_raw_items_hash ON raw_items(content_hash);

-- Raw payloads are kept separately so they can be pruned for disk space
-- without touching the immutable history.
CREATE TABLE raw_payloads (
    raw_item_id     INTEGER PRIMARY KEY REFERENCES raw_items(id),
    payload_gz      BLOB NOT NULL,
    stored_at       TEXT NOT NULL
);

-- Time series of measurements. Append-only.
CREATE TABLE observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       TEXT NOT NULL REFERENCES sources(id),
    subject_type    TEXT NOT NULL,          -- repo | story | model | space | protocol | chain | market | network | feed | paper_category | mcp_registry
    subject_key     TEXT NOT NULL,          -- e.g. "owner/name", "hn:123", "llama:aave"
    metric          TEXT NOT NULL,          -- stars | forks | points | comments | tvl_usd | volume_24h_usd ...
    value           REAL NOT NULL,
    observed_at     TEXT NOT NULL,          -- time the value refers to
    available_at    TEXT NOT NULL,          -- earliest time we could have known it
    ingested_at     TEXT NOT NULL,          -- when the row was written
    is_backfill     INTEGER NOT NULL DEFAULT 0,
    raw_item_id     INTEGER REFERENCES raw_items(id),
    run_id          INTEGER REFERENCES collection_runs(id),
    CHECK (available_at >= observed_at)
);
CREATE INDEX idx_obs_subject ON observations(subject_type, subject_key, metric, available_at);
CREATE INDEX idx_obs_available ON observations(available_at);
-- Prevent duplicate rows for the same measurement instant.
CREATE UNIQUE INDEX uq_obs_point ON observations(source_id, subject_type, subject_key, metric, observed_at, is_backfill);

CREATE TRIGGER observations_no_update BEFORE UPDATE ON observations
BEGIN SELECT RAISE(ABORT, 'observations are append-only'); END;
CREATE TRIGGER observations_no_delete BEFORE DELETE ON observations
BEGIN SELECT RAISE(ABORT, 'observations are append-only'); END;
CREATE TRIGGER raw_items_no_update BEFORE UPDATE ON raw_items
BEGIN SELECT RAISE(ABORT, 'raw_items are append-only'); END;
CREATE TRIGGER raw_items_no_delete BEFORE DELETE ON raw_items
BEGIN SELECT RAISE(ABORT, 'raw_items are append-only'); END;

-- Every external request / LLM call / post, with its cost.
CREATE TABLE api_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    utc_day         TEXT NOT NULL,          -- YYYY-MM-DD (UTC) for daily caps
    provider        TEXT NOT NULL,
    endpoint        TEXT NOT NULL,
    operation       TEXT NOT NULL,
    units           REAL NOT NULL DEFAULT 1,
    cost_usd        REAL NOT NULL DEFAULT 0,
    status_code     INTEGER,
    run_id          INTEGER REFERENCES collection_runs(id),
    detail          TEXT
);
CREATE INDEX idx_usage_day ON api_usage(provider, utc_day);
CREATE TRIGGER api_usage_no_update BEFORE UPDATE ON api_usage
BEGIN SELECT RAISE(ABORT, 'api_usage is append-only'); END;
CREATE TRIGGER api_usage_no_delete BEFORE DELETE ON api_usage
BEGIN SELECT RAISE(ABORT, 'api_usage is append-only'); END;
