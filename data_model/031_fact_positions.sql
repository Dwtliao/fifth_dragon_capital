-- Fact table: point-in-time position snapshots.
-- Grain enforced: one row per (account, symbol, fetched_at).
-- symbol is NOT NULL — every position in E*TRADE data has a ticker or CUSIP.
-- For daily reporting, collapse with MAX(fetched_at) per date if multiple
-- intraday fetches exist.
CREATE TABLE IF NOT EXISTS fact_positions (
    id                 SERIAL PRIMARY KEY,
    date_key           DATE NOT NULL REFERENCES dim_dates(date_key),
    account_sk         INT  NOT NULL REFERENCES dim_accounts(account_sk),
    account_id_key     TEXT NOT NULL,
    symbol             TEXT NOT NULL REFERENCES dim_symbols(symbol),
    fetched_at         TIMESTAMPTZ NOT NULL,
    quantity           NUMERIC,
    cost_per_share     NUMERIC,
    total_cost         NUMERIC,
    market_value       NUMERIC,
    unrealized_pnl     NUMERIC,
    unrealized_pnl_pct NUMERIC,
    pct_of_portfolio   NUMERIC,
    UNIQUE (account_id_key, symbol, fetched_at)
);

CREATE INDEX IF NOT EXISTS fact_pos_date    ON fact_positions (date_key);
CREATE INDEX IF NOT EXISTS fact_pos_symbol  ON fact_positions (symbol);
CREATE INDEX IF NOT EXISTS fact_pos_account ON fact_positions (account_sk);
