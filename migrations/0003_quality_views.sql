-- 0003_quality_views: derived (non-destructive) quality flags as views.
-- Nothing here modifies stored history.

-- The first live run of each source captures the provider's *existing* catalog
-- (e.g. all DeFiLlama protocols, MCP servers). Those items are a baseline, not
-- new activity, and must not be counted as a spike by scoring.
CREATE VIEW v_initial_snapshot_runs AS
SELECT source_id, MIN(run_id) AS run_id
FROM raw_items
WHERE is_backfill = 0 AND run_id IS NOT NULL
GROUP BY source_id;

CREATE VIEW v_raw_items AS
SELECT r.*,
       CASE WHEN r.is_backfill = 0 AND r.run_id = s.run_id THEN 1 ELSE 0 END AS is_initial_snapshot
FROM raw_items r
LEFT JOIN v_initial_snapshot_runs s ON s.source_id = r.source_id;

-- GitHub clone farms: the same repository name published by several different
-- owners (a common pattern for spam / malware lures). Flagged, never deleted.
CREATE VIEW v_repo_clone_clusters AS
SELECT lower(substr(external_id, instr(external_id, '/') + 1)) AS repo_name,
       COUNT(*) AS owners,
       MIN(first_seen_at) AS first_seen_at
FROM raw_items
WHERE source_id = 'github' AND item_type = 'repo'
GROUP BY repo_name
HAVING COUNT(*) >= 3;

CREATE VIEW v_repo_quality AS
SELECT r.id AS raw_item_id,
       r.external_id,
       CASE WHEN c.repo_name IS NOT NULL THEN 1 ELSE 0 END AS suspected_clone_farm,
       c.owners AS clone_owner_count
FROM raw_items r
LEFT JOIN v_repo_clone_clusters c
       ON c.repo_name = lower(substr(r.external_id, instr(r.external_id, '/') + 1))
WHERE r.source_id = 'github' AND r.item_type = 'repo';
