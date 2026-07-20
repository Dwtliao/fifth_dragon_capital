-- Must run before 052_mv_portfolio_timeseries.sql and
-- 056_mv_portfolio_timeseries_by_account.sql, which reference this table —
-- CREATE MATERIALIZED VIEW IF NOT EXISTS still parses/analyzes the query body
-- even when skipped, so a fresh create_tables()/migrate() bootstrap would
-- fail on those views if this table doesn't exist yet at that point in the
-- sorted file order. See 014b_vehicle_type_columns.sql for the same pattern.
--
-- Reconstructed daily portfolio value for trading days lost to the
-- 2026-07-01 -> 2026-07-19 daily_sync outage (issue #67). Market value only —
-- no cost basis / unrealized P&L, since a correct historical value for those
-- requires re-running FIFO lot-matching as of each past date (out of scope).
-- Never a substitute for `positions`; that table stays raw E*TRADE truth.
--
-- repriced_market_value / flat_carry_market_value split out for audit: which
-- part of total_market_value came from an actual yfinance historical close
-- (EQ/ETF holdings) vs. carried forward unchanged from the source snapshot
-- (BOND/MUTUALFUND/MMF/unknown holdings, where a daily reprice either isn't
-- available or isn't a meaningful concept — see scripts/backfill_portfolio_history.py).
CREATE TABLE IF NOT EXISTS portfolio_value_backfill (
    account_id_key           TEXT NOT NULL,
    date                     DATE NOT NULL,
    total_market_value       NUMERIC NOT NULL,
    repriced_market_value    NUMERIC NOT NULL DEFAULT 0,
    flat_carry_market_value  NUMERIC NOT NULL DEFAULT 0,
    source_snapshot_date     DATE NOT NULL,
    valuation_mode           TEXT NOT NULL DEFAULT 'reprice_eq_etf_flat_carry_rest',
    created_at               TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (account_id_key, date)
);
