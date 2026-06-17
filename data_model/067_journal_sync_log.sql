-- Tracks which journal files have been processed by journal_sync.py.
-- file_mtime lets us detect when a file has been updated since last sync.
CREATE TABLE IF NOT EXISTS journal_sync_log (
    id           SERIAL PRIMARY KEY,
    file_path    TEXT        NOT NULL UNIQUE,
    file_mtime   FLOAT       NOT NULL,   -- os.path.getmtime() value
    synced_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    positions_updated  INT  DEFAULT 0,
    watch_updated      INT  DEFAULT 0,
    alerts_created     INT  DEFAULT 0,
    dry_run      BOOLEAN     NOT NULL DEFAULT FALSE
);
