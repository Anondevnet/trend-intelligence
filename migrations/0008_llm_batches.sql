-- 0008: Stage 8 -- Message Batches bookkeeping. Append-only. A batch is recorded when
-- submitted; its per-request outcomes are recorded once when collected (UNIQUE guards
-- against double collection, and therefore double cost recording).

CREATE TABLE llm_batches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id            TEXT NOT NULL UNIQUE,      -- Anthropic batch id (or a mock id in tests)
    task                TEXT NOT NULL,
    tier                TEXT NOT NULL,
    model               TEXT NOT NULL,
    n_requests          INTEGER NOT NULL,
    custom_ids_json     TEXT NOT NULL,
    estimated_cost_usd  REAL NOT NULL,
    submitted_at        TEXT NOT NULL
);

CREATE TABLE llm_batch_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        TEXT NOT NULL REFERENCES llm_batches(batch_id),
    custom_id       TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('ok', 'invalid', 'errored', 'canceled', 'expired')),
    output_json     TEXT,                          -- validated structured output, if ok
    flag_reason     TEXT,                          -- why it needs human review, if not ok
    cost_usd        REAL NOT NULL DEFAULT 0,
    collected_at    TEXT NOT NULL,
    UNIQUE (batch_id, custom_id)
);

CREATE TRIGGER llm_batches_no_update BEFORE UPDATE ON llm_batches
BEGIN SELECT RAISE(ABORT, 'llm_batches are append-only'); END;
CREATE TRIGGER llm_batches_no_delete BEFORE DELETE ON llm_batches
BEGIN SELECT RAISE(ABORT, 'llm_batches are append-only'); END;
CREATE TRIGGER llm_batch_results_no_update BEFORE UPDATE ON llm_batch_results
BEGIN SELECT RAISE(ABORT, 'llm_batch_results are append-only'); END;
CREATE TRIGGER llm_batch_results_no_delete BEFORE DELETE ON llm_batch_results
BEGIN SELECT RAISE(ABORT, 'llm_batch_results are append-only'); END;
