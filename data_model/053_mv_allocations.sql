-- Materialized view: current portfolio allocations by position.
-- One row per (account, symbol) from the latest positions snapshot.
-- Override priority: dim_symbol_overrides > dim_symbols (yfinance) > fallback.
-- Dollar columns are ROUND(..., 2). pct_of_portfolio is left as double precision.
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_allocations AS
WITH latest_positions AS (
    SELECT
        account_id_key,
        symbol,
        security_type,
        quantity,
        total_cost,
        market_value
    FROM positions
    WHERE fetched_at = (SELECT MAX(fetched_at) FROM positions)
),
portfolio_total AS (
    SELECT SUM(market_value) AS total_mv FROM latest_positions
)
SELECT
    p.account_id_key,
    p.symbol,
    COALESCE(o.sector,       s.sector,      'Unknown')                      AS sector,
    COALESCE(o.asset_class,  s.asset_class, p.security_type, 'Unknown')     AS asset_class,
    COALESCE(o.vehicle_type, s.vehicle_type, p.security_type, 'Unknown')    AS vehicle_type,
    COALESCE(
        (SELECT array_agg(t.tag ORDER BY t.tag)
         FROM symbol_exposure_tags t WHERE t.symbol = p.symbol),
        ARRAY[]::text[]
    )                                                                        AS exposure_tags,
    p.security_type,
    p.quantity,
    ROUND(p.total_cost::numeric, 2)                                          AS cost_basis,
    ROUND(p.market_value::numeric, 2)                                        AS market_value,
    p.market_value / NULLIF(pt.total_mv, 0) * 100                            AS pct_of_portfolio,
    ROUND((p.market_value - p.total_cost)::numeric, 2)                       AS unrealized_pnl
FROM latest_positions p
CROSS JOIN portfolio_total pt
LEFT JOIN dim_symbols s          ON s.symbol = p.symbol
LEFT JOIN dim_symbol_overrides o ON o.symbol = p.symbol
ORDER BY p.market_value DESC NULLS LAST;

CREATE UNIQUE INDEX IF NOT EXISTS mv_allocations_pk
    ON mv_allocations (account_id_key, symbol);
