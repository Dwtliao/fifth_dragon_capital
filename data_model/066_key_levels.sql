CREATE TABLE IF NOT EXISTS key_levels (
    id          SERIAL PRIMARY KEY,
    section     TEXT    NOT NULL CHECK (section IN ('positions', 'watch')),
    ticker      TEXT    NOT NULL,
    stop        NUMERIC,
    support     NUMERIC,
    resistance  NUMERIC,
    alert_above NUMERIC,
    note        TEXT,
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (section, ticker)
);
