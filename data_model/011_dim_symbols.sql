-- Symbol metadata dimension: one row per security.
-- Ticker symbols are used as the primary key for simplicity, but options,
-- bonds, and delisted tickers can share tickers over time. The cusip column
-- provides a stable identifier for fixed-income and where available for equities.
-- Seeded from yfinance; CUSIPs are set directly without API lookup.
CREATE TABLE IF NOT EXISTS dim_symbols (
    symbol      TEXT PRIMARY KEY,
    cusip       TEXT,                   -- 9-char stable identifier; populated for bonds
    name        TEXT,
    sector      TEXT,                   -- Technology | Energy | Materials | etc.
    industry    TEXT,
    asset_class TEXT,                   -- Equity | ETF | Bond | Option | Cash | Other
    currency    TEXT DEFAULT 'USD',
    exchange    TEXT,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS dim_symbols_cusip ON dim_symbols (cusip);
