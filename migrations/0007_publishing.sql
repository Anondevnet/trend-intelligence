-- 0007: Stage 10 groundwork -- post approval queue and audit log. Both tables append-only.
-- A draft's content never changes (an edit is a new draft that supersedes it). Its state is
-- derived from post_events, which is also the audit log. Triggers enforce, inside SQLite,
-- that nothing can be recorded as published without a human approval of the exact content.

CREATE TABLE post_drafts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL,
    created_by      TEXT NOT NULL,          -- human:<name> or system:<component>
    text            TEXT NOT NULL,
    content_hash    TEXT NOT NULL,          -- sha256 over text + sources + claim_status + sponsored
    claim_status    TEXT NOT NULL CHECK (claim_status IN ('verified', 'speculative', 'rumor')),
    sponsored       INTEGER NOT NULL DEFAULT 0 CHECK (sponsored IN (0, 1)),
    sources_json    TEXT NOT NULL,          -- [{"raw_item_id": n} | {"url": "https://..."}]
    trend_id        INTEGER REFERENCES trends(id),
    supersedes      INTEGER REFERENCES post_drafts(id),
    meta_json       TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE post_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              TEXT NOT NULL,
    draft_id        INTEGER REFERENCES post_drafts(id),   -- NULL for kill-switch events
    event           TEXT NOT NULL CHECK (event IN (
                        'created', 'gate_passed', 'gate_failed', 'approved', 'rejected',
                        'publish_blocked', 'publish_attempt', 'published_dry_run',
                        'published_live', 'publish_failed', 'kill_switch_on', 'kill_switch_off')),
    actor           TEXT NOT NULL,
    content_hash    TEXT,
    detail_json     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_post_events_draft ON post_events(draft_id, id);
CREATE INDEX idx_post_events_event_at ON post_events(event, at);
-- One publish attempt per draft, ever. A crash mid-send leaves the draft stuck in
-- 'publishing' rather than risking a double post.
CREATE UNIQUE INDEX uq_post_attempt ON post_events(draft_id) WHERE event = 'publish_attempt';
CREATE UNIQUE INDEX uq_post_published ON post_events(draft_id)
    WHERE event IN ('published_dry_run', 'published_live');

CREATE TRIGGER post_drafts_no_update BEFORE UPDATE ON post_drafts
BEGIN SELECT RAISE(ABORT, 'post_drafts are append-only'); END;
CREATE TRIGGER post_drafts_no_delete BEFORE DELETE ON post_drafts
BEGIN SELECT RAISE(ABORT, 'post_drafts are append-only'); END;
CREATE TRIGGER post_events_no_update BEFORE UPDATE ON post_events
BEGIN SELECT RAISE(ABORT, 'post_events are append-only'); END;
CREATE TRIGGER post_events_no_delete BEFORE DELETE ON post_events
BEGIN SELECT RAISE(ABORT, 'post_events are append-only'); END;

-- Approval: by a human, for the draft's exact content, after a passing gate run.
CREATE TRIGGER post_events_approval_rules BEFORE INSERT ON post_events
WHEN NEW.event = 'approved'
BEGIN
    SELECT RAISE(ABORT, 'approval must come from a human actor')
    WHERE NEW.actor NOT LIKE 'human:_%';
    SELECT RAISE(ABORT, 'approval does not match the draft content')
    WHERE NEW.content_hash IS NOT (SELECT content_hash FROM post_drafts WHERE id = NEW.draft_id);
    SELECT RAISE(ABORT, 'draft has not passed the quality gate')
    WHERE NOT EXISTS (
        SELECT 1 FROM post_events g
        WHERE g.draft_id = NEW.draft_id AND g.event = 'gate_passed'
          AND g.content_hash = NEW.content_hash
          AND NOT EXISTS (SELECT 1 FROM post_events f WHERE f.draft_id = NEW.draft_id
                          AND f.event = 'gate_failed' AND f.id > g.id));
END;

-- Publishing: needs a standing approval of the exact content (no later rejection).
CREATE TRIGGER post_events_publish_rules BEFORE INSERT ON post_events
WHEN NEW.event IN ('publish_attempt', 'published_dry_run', 'published_live')
BEGIN
    SELECT RAISE(ABORT, 'publishing requires a standing human approval of this content')
    WHERE NOT EXISTS (
        SELECT 1 FROM post_events a
        WHERE a.draft_id = NEW.draft_id AND a.event = 'approved'
          AND a.content_hash = NEW.content_hash
          AND a.content_hash = (SELECT content_hash FROM post_drafts WHERE id = NEW.draft_id)
          AND NOT EXISTS (SELECT 1 FROM post_events r WHERE r.draft_id = NEW.draft_id
                          AND r.event = 'rejected' AND r.id > a.id));
    SELECT RAISE(ABORT, 'a publish result needs a prior publish attempt')
    WHERE NEW.event IN ('published_dry_run', 'published_live')
      AND NOT EXISTS (SELECT 1 FROM post_events p WHERE p.draft_id = NEW.draft_id
                      AND p.event = 'publish_attempt');
END;
