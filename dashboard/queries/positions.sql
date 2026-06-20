-- ============================================================
-- Positions queries (unrealized P/L, current holdings)
-- Sources: mv_unrealized_pnl, positions, balances
-- Run: psql $DATABASE_URL -f positions.sql
-- ============================================================


-- 1. Current unrealized P/L — all positions ranked by P/L
-- ------------------------------------------------------------
SELECT
    account_id_key,
    symbol,
    security_type,
    quantity,
    round(cost_basis::numeric, 2)                   AS cost_basis,
    round(market_value::numeric, 2)                 AS market_value,
    round(unrealized_pnl::numeric, 2)               AS unrealized_pnl,
    unrealized_pnl_pct                              AS pnl_pct,
    as_of
FROM mv_unrealized_pnl
ORDER BY unrealized_pnl DESC NULLS LAST;


-- 2. Portfolio totals (unrealized)
-- ------------------------------------------------------------
SELECT
    round(sum(cost_basis)::numeric, 2)              AS total_cost_basis,
    round(sum(market_value)::numeric, 2)            AS total_market_value,
    round(sum(unrealized_pnl)::numeric, 2)          AS total_unrealized_pnl,
    round(
        sum(unrealized_pnl) / nullif(sum(cost_basis), 0) * 100
    , 2)                                            AS unrealized_pnl_pct,
    max(as_of)                                      AS as_of
FROM mv_unrealized_pnl;


-- 3. Current account balances (latest snapshot per account)
-- ------------------------------------------------------------
SELECT
    b.account_id_key,
    round(b.cash_balance::numeric, 2)               AS cash_balance,
    round(b.net_mv::numeric, 2)                     AS invested_value,
    round(b.total_account_value::numeric, 2)        AS total_account_value,
    b.fetched_at
FROM balances b
WHERE (b.account_id_key, b.fetched_at) IN (
    SELECT account_id_key, MAX(fetched_at) FROM balances GROUP BY account_id_key
)
ORDER BY b.account_id_key;


-- 4. Position history — how many snapshots exist and date range
-- ------------------------------------------------------------
SELECT
    count(DISTINCT fetched_at)                      AS snapshots,
    min(fetched_at)::date                           AS earliest,
    max(fetched_at)::date                           AS latest
FROM positions;


-- 5. Top 10 positions by market value (current snapshot)
-- ------------------------------------------------------------
SELECT
    symbol,
    security_type,
    quantity,
    round(market_value::numeric, 2)                 AS market_value,
    round(unrealized_pnl::numeric, 2)               AS unrealized_pnl,
    unrealized_pnl_pct                              AS pnl_pct
FROM mv_unrealized_pnl
ORDER BY market_value DESC NULLS LAST
LIMIT 10;
