-- Strategy tags for realized trades.
-- One tag per (account, symbol, realized_gain_id) combination.
-- realized_gain_id links to a specific FIFO-matched lot in realized_gains.
CREATE TABLE IF NOT EXISTS trade_tags (
    id               SERIAL PRIMARY KEY,
    account_id_key   TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    realized_gain_id INTEGER REFERENCES realized_gains(id) ON DELETE CASCADE,
    tag              TEXT NOT NULL,
    notes            TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (realized_gain_id, tag)
);

CREATE INDEX IF NOT EXISTS trade_tags_account_symbol ON trade_tags (account_id_key, symbol);
CREATE INDEX IF NOT EXISTS trade_tags_tag            ON trade_tags (tag);
