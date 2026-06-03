-- Per-account daily portfolio timeseries.
-- Same shape as mv_portfolio_timeseries but with account_id_key as a dimension.
-- Used by the Performance page when a single account is selected.
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_portfolio_timeseries_by_account AS
WITH latest_per_account_per_day AS (
    SELECT
        account_id_key,
        fetched_at::date AS date,
        MAX(fetched_at)  AS latest_ts
    FROM positions
    GROUP BY account_id_key, fetched_at::date
),
daily_values AS (
    SELECT
        lp.account_id_key,
        lp.date,
        ROUND(SUM(p.market_value)::numeric, 2)              AS total_market_value,
        ROUND(SUM(p.total_cost)::numeric, 2)                AS total_cost_basis,
        ROUND(SUM(p.market_value - p.total_cost)::numeric, 2) AS total_unrealized_pnl
    FROM latest_per_account_per_day lp
    JOIN positions p
      ON p.account_id_key = lp.account_id_key
     AND p.fetched_at     = lp.latest_ts
    GROUP BY lp.account_id_key, lp.date
),
with_returns AS (
    SELECT
        account_id_key,
        date,
        total_market_value,
        total_cost_basis,
        total_unrealized_pnl,
        (total_market_value / NULLIF(LAG(total_market_value) OVER (
            PARTITION BY account_id_key ORDER BY date
        ), 0) - 1) * 100 AS daily_return_pct,
        MAX(total_market_value) OVER (
            PARTITION BY account_id_key
            ORDER BY date
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS running_peak
    FROM daily_values
)
SELECT
    r.account_id_key,
    r.date,
    r.total_market_value,
    r.total_cost_basis,
    r.total_unrealized_pnl,
    r.daily_return_pct,
    (r.total_market_value / NULLIF(v7.total_market_value,  0) - 1) * 100 AS rolling_7d_return_pct,
    (r.total_market_value / NULLIF(v30.total_market_value, 0) - 1) * 100 AS rolling_30d_return_pct,
    (r.total_market_value / NULLIF(v90.total_market_value, 0) - 1) * 100 AS rolling_90d_return_pct,
    STDDEV(r.daily_return_pct) OVER (
        PARTITION BY r.account_id_key
        ORDER BY r.date
        ROWS BETWEEN 29 PRECEDING AND CURRENT ROW
    ) * SQRT(252) AS rolling_volatility_30d,
    (r.total_market_value - r.running_peak) / NULLIF(r.running_peak, 0) * 100 AS drawdown_from_peak_pct
FROM with_returns r
LEFT JOIN daily_values v7
  ON v7.account_id_key = r.account_id_key AND v7.date = r.date - INTERVAL '7 days'
LEFT JOIN daily_values v30
  ON v30.account_id_key = r.account_id_key AND v30.date = r.date - INTERVAL '30 days'
LEFT JOIN daily_values v90
  ON v90.account_id_key = r.account_id_key AND v90.date = r.date - INTERVAL '90 days'
ORDER BY r.account_id_key, r.date;

CREATE UNIQUE INDEX IF NOT EXISTS mv_portfolio_timeseries_by_account_pk
    ON mv_portfolio_timeseries_by_account (account_id_key, date);
