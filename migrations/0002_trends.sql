-- 0002_trends: trend identity, aliases, and item->trend mentions.

CREATE TABLE trends (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    category            TEXT NOT NULL,
    kind                TEXT NOT NULL,          -- narrative (taxonomy) | entity (repo/model/protocol/...)
    origin              TEXT NOT NULL,          -- taxonomy | github | huggingface | defillama | polymarket | mcp_registry
    subject_type        TEXT,                   -- links entity trends to observations.subject_type
    subject_key         TEXT,                   -- ... and observations.subject_key
    created_at          TEXT NOT NULL,
    first_detected_at   TEXT                    -- earliest available_at of any linked mention; set once
);
CREATE INDEX idx_trends_subject ON trends(subject_type, subject_key);

CREATE TRIGGER trends_first_detected_set_once BEFORE UPDATE OF first_detected_at ON trends
WHEN OLD.first_detected_at IS NOT NULL AND NEW.first_detected_at > OLD.first_detected_at
BEGIN SELECT RAISE(ABORT, 'first_detected_at can only move earlier'); END;

CREATE TABLE trend_aliases (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trend_id            INTEGER NOT NULL REFERENCES trends(id),
    alias               TEXT NOT NULL,
    alias_norm          TEXT NOT NULL,
    match_mode          TEXT NOT NULL,          -- phrase (case-insensitive, word-bounded) | exact (case-sensitive) | url
    requires_context    TEXT,                   -- JSON list; at least one must co-occur (disambiguation)
    created_at          TEXT NOT NULL,
    UNIQUE (trend_id, alias_norm, match_mode)
);

-- Which items mention which trends. Append-only; versioned by linker so
-- re-linking with a better matcher adds rows instead of rewriting history.
CREATE TABLE trend_mentions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    trend_id            INTEGER NOT NULL REFERENCES trends(id),
    raw_item_id         INTEGER NOT NULL REFERENCES raw_items(id),
    source_id           TEXT NOT NULL,
    alias_id            INTEGER REFERENCES trend_aliases(id),
    available_at        TEXT NOT NULL,          -- = raw_items.available_at
    linked_at           TEXT NOT NULL,
    linker_version      TEXT NOT NULL,
    UNIQUE (trend_id, raw_item_id, linker_version)
);
CREATE INDEX idx_mentions_trend ON trend_mentions(trend_id, available_at);
CREATE TRIGGER trend_mentions_no_update BEFORE UPDATE ON trend_mentions
BEGIN SELECT RAISE(ABORT, 'trend_mentions are append-only'); END;
CREATE TRIGGER trend_mentions_no_delete BEFORE DELETE ON trend_mentions
BEGIN SELECT RAISE(ABORT, 'trend_mentions are append-only'); END;

-- Watermark of which raw_items each linker version has processed.
CREATE TABLE linker_state (
    linker_version      TEXT PRIMARY KEY,
    last_raw_item_id    INTEGER NOT NULL,
    updated_at          TEXT NOT NULL
);
