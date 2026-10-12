-- Additive approval boundary; no changes to live alerts or key levels.
CREATE TABLE IF NOT EXISTS journal_review_items (
    id BIGSERIAL PRIMARY KEY,
    idea_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('context','watch','request','stop')),
    ticker TEXT NOT NULL,
    condition TEXT,
    threshold NUMERIC,
    timeframe TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL,
    source_date DATE,
    validation_error TEXT,
    needs_clarification BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL CHECK (status IN ('context','idea','pending','blocked','accepted','rejected')),
    revision INTEGER NOT NULL DEFAULT 1,
    alert_id INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS journal_review_status_idx ON journal_review_items(status,ticker);
CREATE TABLE IF NOT EXISTS journal_review_evidence (
    id BIGSERIAL PRIMARY KEY,
    item_id BIGINT NOT NULL REFERENCES journal_review_items(id),
    file_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    evidence TEXT NOT NULL,
    source_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(item_id,file_path,content_hash,evidence)
);
CREATE TABLE IF NOT EXISTS journal_review_decisions (
    id BIGSERIAL PRIMARY KEY,
    item_id BIGINT NOT NULL REFERENCES journal_review_items(id),
    action TEXT NOT NULL,
    details JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE journal_sync_log ADD COLUMN IF NOT EXISTS extraction_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE journal_sync_log ADD COLUMN IF NOT EXISTS proposal_counts JSONB NOT NULL DEFAULT '{}';
ALTER TABLE journal_sync_log ADD COLUMN IF NOT EXISTS extracted_json JSONB;
