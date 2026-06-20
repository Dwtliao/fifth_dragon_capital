-- Materialized view: current unrealized P/L from the latest positions snapshot.
-- Refresh after every positions sync with: REFRESH MATERIALIZED VIEW mv_unrealized_pnl;
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_unrealized_pnl AS
SELECT
    p.account_id_key,
    p.symbol,
    p.security_type,
    p.quantity,
    p.total_cost                                        AS cost_basis,
    p.market_value,
    p.market_value - p.total_cost                       AS unrealized_pnl,
    CASE
        WHEN p.total_cost IS NOT NULL AND p.total_cost != 0
        THEN ROUND((p.market_value - p.total_cost) / p.total_cost * 100, 2)
        ELSE NULL
    END                                                 AS unrealized_pnl_pct,
    p.fetched_at                                        AS as_of
FROM (
    SELECT
        account_id_key,
        symbol,
        MAX(security_type)  AS security_type,
        SUM(quantity)       AS quantity,
        SUM(total_cost)     AS total_cost,
        SUM(market_value)   AS market_value,
        MAX(fetched_at)     AS fetched_at
    FROM positions
    WHERE (account_id_key, fetched_at) IN (
        SELECT account_id_key, MAX(fetched_at) FROM positions GROUP BY account_id_key
    )
    GROUP BY account_id_key, symbol
) p;

CREATE UNIQUE INDEX IF NOT EXISTS mv_unrealized_pnl_pk
    ON mv_unrealized_pnl (account_id_key, symbol);
