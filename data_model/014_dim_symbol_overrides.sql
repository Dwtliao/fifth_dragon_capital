-- Manual overrides for symbol metadata.
-- Values here take precedence over yfinance data in mv_allocations.
-- Managed via the Symbol Admin dashboard page.
CREATE TABLE IF NOT EXISTS dim_symbol_overrides (
    symbol      TEXT PRIMARY KEY,
    sector      TEXT REFERENCES dim_sectors(sector),
    asset_class TEXT,
    name        TEXT,
    notes       TEXT,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
