-- Sector and asset class attribution timeseries.
-- One row per (date, account_id_key, sector, asset_class) from daily position snapshots.
-- Override priority matches mv_allocations: dim_symbol_overrides > dim_symbols > fallback.
-- Used by the Performance page attribution drill-down tabs.
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_attribution_timeseries AS
WITH latest_per_account_per_day AS (
    SELECT
        account_id_key,
        fetched_at::date AS date,
        MAX(fetched_at)  AS latest_ts
    FROM positions
    GROUP BY account_id_key, fetched_at::date
),
attributed AS (
    SELECT
        lp.date,
        p.account_id_key,
        COALESCE(o.sector,     s.sector,     'Unknown') AS sector,
        COALESCE(o.asset_class, s.asset_class, p.security_type, 'Unknown') AS asset_class,
        ROUND(SUM(p.market_value)::numeric, 2)              AS market_value,
        ROUND(SUM(p.total_cost)::numeric, 2)                AS cost_basis,
        ROUND(SUM(p.market_value - p.total_cost)::numeric, 2) AS unrealized_pnl
    FROM latest_per_account_per_day lp
    JOIN positions p
      ON p.account_id_key = lp.account_id_key
     AND p.fetched_at     = lp.latest_ts
    LEFT JOIN dim_symbols s          ON s.symbol = p.symbol
    LEFT JOIN dim_symbol_overrides o ON o.symbol = p.symbol
    GROUP BY
        lp.date,
        p.account_id_key,
        COALESCE(o.sector,     s.sector,     'Unknown'),
        COALESCE(o.asset_class, s.asset_class, p.security_type, 'Unknown')
),
with_weights AS (
    SELECT
        a.*,
        a.market_value / NULLIF(SUM(a.market_value) OVER (
            PARTITION BY a.date, a.account_id_key
        ), 0) * 100 AS pct_of_account
    FROM attributed a
)
SELECT *
FROM with_weights
ORDER BY account_id_key, date, sector, asset_class;

CREATE UNIQUE INDEX IF NOT EXISTS mv_attribution_timeseries_pk
    ON mv_attribution_timeseries (date, account_id_key, sector, asset_class);
