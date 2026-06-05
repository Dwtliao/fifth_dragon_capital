-- Daily closing prices for benchmark symbols (SPY, etc.).
-- Populated by etrade_sync.analytics.prices.seed_prices() via yfinance.
-- Prices are adjusted close (splits and dividends factored in).
CREATE TABLE IF NOT EXISTS market_prices (
    id      SERIAL PRIMARY KEY,
    symbol  TEXT NOT NULL,
    date    DATE NOT NULL,
    close   NUMERIC NOT NULL,
    UNIQUE (symbol, date)
);

CREATE INDEX IF NOT EXISTS market_prices_symbol_date ON market_prices (symbol, date);
