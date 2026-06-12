-- Physical precious metals holdings — fully separate from E*TRADE schema.
-- account_name + location are free-text; no FK to accounts table.
CREATE TABLE IF NOT EXISTS physical_holdings_pm (
    id              SERIAL PRIMARY KEY,
    account_name    TEXT    NOT NULL,
    location        TEXT    NOT NULL,
    metal           TEXT    NOT NULL CHECK (metal IN ('gold', 'silver', 'platinum', 'palladium')),
    weight_oz       NUMERIC NOT NULL CHECK (weight_oz > 0),
    purchase_price  NUMERIC NOT NULL CHECK (purchase_price >= 0),  -- total USD paid
    purchase_date   DATE    NOT NULL,
    description     TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Daily spot closes entered manually from the P6 dashboard.
CREATE TABLE IF NOT EXISTS physical_prices_pm (
    metal       TEXT    NOT NULL CHECK (metal IN ('gold', 'silver', 'platinum', 'palladium')),
    price_date  DATE    NOT NULL,
    spot_price  NUMERIC NOT NULL CHECK (spot_price > 0),
    PRIMARY KEY (metal, price_date)
);
