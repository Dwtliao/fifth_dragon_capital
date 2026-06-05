-- Per-account benchmark comparison vs SPY.
-- Cumulative returns are anchored to each account's first date in the timeseries.
-- Depends on mv_portfolio_timeseries_by_account — refresh that view first.
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_benchmark_comparison_by_account AS
WITH spy AS (
    SELECT
        date,
        close AS spy_close,
        (close / NULLIF(LAG(close) OVER (ORDER BY date), 0) - 1) * 100 AS spy_daily_return_pct
    FROM market_prices
    WHERE symbol = 'SPY'
),
base_values AS (
    SELECT DISTINCT ON (p.account_id_key)
        p.account_id_key,
        p.total_market_value AS portfolio_base,
        s.spy_close          AS spy_base
    FROM mv_portfolio_timeseries_by_account p
    JOIN spy s ON s.date = p.date
    ORDER BY p.account_id_key, p.date
)
SELECT
    p.account_id_key,
    p.date,
    p.total_market_value,
    p.daily_return_pct                AS portfolio_daily_return_pct,
    p.rolling_30d_return_pct          AS rolling_30d_portfolio_pct,
    s.spy_close,
    s.spy_daily_return_pct,
    (s.spy_close / NULLIF(s30.spy_close, 0) - 1) * 100            AS rolling_30d_spy_pct,
    (p.total_market_value / NULLIF(bv.portfolio_base, 0) - 1) * 100 AS portfolio_cumulative_pct,
    (s.spy_close          / NULLIF(bv.spy_base,        0) - 1) * 100 AS spy_cumulative_pct,
    (p.total_market_value / NULLIF(bv.portfolio_base, 0)
     - s.spy_close        / NULLIF(bv.spy_base,        0)) * 100   AS alpha_pct
FROM mv_portfolio_timeseries_by_account p
JOIN  spy s   ON s.date   = p.date
LEFT JOIN spy s30 ON s30.date = p.date - INTERVAL '30 days'
JOIN  base_values bv ON bv.account_id_key = p.account_id_key
ORDER BY p.account_id_key, p.date;

CREATE UNIQUE INDEX IF NOT EXISTS mv_benchmark_comparison_by_account_pk
    ON mv_benchmark_comparison_by_account (account_id_key, date);
