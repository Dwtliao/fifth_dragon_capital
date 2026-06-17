CREATE TABLE IF NOT EXISTS price_alerts (
    id            SERIAL PRIMARY KEY,
    ticker        TEXT    NOT NULL,   -- yfinance format: ^VIX, VIXY, NQ=F, etc.
    label         TEXT,               -- friendly name, e.g. "VIX Fear Spike"
    condition     TEXT    NOT NULL CHECK (condition IN ('above', 'below')),
    threshold     NUMERIC NOT NULL,
    enabled       BOOLEAN NOT NULL DEFAULT TRUE,
    triggered     BOOLEAN NOT NULL DEFAULT FALSE,  -- TRUE = currently firing, suppresses repeat
    last_fired_at TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
