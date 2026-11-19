-- 0004: LLM cost-ledger columns on api_usage. Additive only: existing rows keep their
-- values and get NULL / 0 in the new columns. ALTER TABLE ADD COLUMN does not fire the
-- append-only UPDATE trigger and rewrites no rows.
ALTER TABLE api_usage ADD COLUMN tier TEXT;
ALTER TABLE api_usage ADD COLUMN model TEXT;
ALTER TABLE api_usage ADD COLUMN input_tokens INTEGER;
ALTER TABLE api_usage ADD COLUMN output_tokens INTEGER;
ALTER TABLE api_usage ADD COLUMN cache_read_tokens INTEGER;
ALTER TABLE api_usage ADD COLUMN cache_write_tokens INTEGER;
ALTER TABLE api_usage ADD COLUMN is_batch INTEGER NOT NULL DEFAULT 0 CHECK (is_batch IN (0, 1));
CREATE INDEX idx_usage_tier_day ON api_usage(provider, tier, utc_day);
