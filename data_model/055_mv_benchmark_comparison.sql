-- Materialized view: portfolio return vs SPY benchmark.
-- One row per date in mv_portfolio_timeseries that has a matching SPY close.
-- Cumulative returns are anchored to the first date in the portfolio timeseries.
-- Rolling lookbacks use exact calendar-day joins (NULL if no SPY data that far back).
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_benchmark_comparison AS
WITH spy AS (
    SELECT
        date,
        close                                                   AS spy_close,
        (close / NULLIF(LAG(close) OVER (ORDER BY date), 0) - 1) * 100
                                                                AS spy_daily_return_pct
    FROM market_prices
    WHERE symbol = 'SPY'
),
base_values AS (
    -- Anchor: first date present in portfolio timeseries
    SELECT
        pt.total_market_value   AS portfolio_base,
        s.spy_close             AS spy_base
    FROM mv_portfolio_timeseries pt
    JOIN spy s ON s.date = pt.date
    ORDER BY pt.date
    LIMIT 1
)
SELECT
    pt.date,
    pt.total_market_value,
    pt.daily_return_pct                                         AS portfolio_daily_return_pct,
    pt.rolling_30d_return_pct                                   AS rolling_30d_portfolio_pct,
    s.spy_close,
    s.spy_daily_return_pct,
    (s.spy_close / NULLIF(s30.spy_close, 0) - 1) * 100         AS rolling_30d_spy_pct,
    -- Cumulative returns from first portfolio date (both normalised to same start)
    (pt.total_market_value / NULLIF(bv.portfolio_base, 0) - 1) * 100
                                                                AS portfolio_cumulative_pct,
    (s.spy_close / NULLIF(bv.spy_base, 0) - 1) * 100           AS spy_cumulative_pct,
    -- Alpha: portfolio outperformance vs SPY (cumulative basis)
    (pt.total_market_value / NULLIF(bv.portfolio_base, 0)
     - s.spy_close        / NULLIF(bv.spy_base,        0)) * 100
                                                                AS alpha_pct
FROM mv_portfolio_timeseries pt
JOIN  spy s   ON s.date   = pt.date
LEFT JOIN spy s30 ON s30.date = pt.date - INTERVAL '30 days'
CROSS JOIN base_values bv
ORDER BY pt.date;

CREATE UNIQUE INDEX IF NOT EXISTS mv_benchmark_comparison_pk
    ON mv_benchmark_comparison (date);
