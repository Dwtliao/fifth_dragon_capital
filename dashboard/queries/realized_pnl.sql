-- ============================================================
-- Realized P/L queries
-- Source table: realized_gains (rebuilt after every full sync)
-- Run: psql $DATABASE_URL -f realized_pnl.sql
-- ============================================================


-- 1. Summary by short-term vs long-term
-- ------------------------------------------------------------
SELECT
    term,
    count(*)                                        AS lots,
    round(sum(cost_basis)::numeric, 2)              AS total_cost,
    round(sum(proceeds)::numeric, 2)                AS total_proceeds,
    round(sum(realized_pnl)::numeric, 2)            AS total_pnl,
    round(avg(holding_days))                        AS avg_holding_days
FROM realized_gains
GROUP BY term
ORDER BY term;


-- 2. P/L by symbol (ranked best to worst)
-- ------------------------------------------------------------
SELECT
    symbol,
    count(*)                                        AS lots,
    round(sum(realized_pnl)::numeric, 2)            AS total_pnl,
    round(sum(cost_basis)::numeric, 2)              AS total_cost,
    round(sum(proceeds)::numeric, 2)                AS total_proceeds,
    round(
        sum(realized_pnl) / nullif(sum(cost_basis), 0) * 100
    , 2)                                            AS pnl_pct,
    round(avg(holding_days))                        AS avg_holding_days
FROM realized_gains
GROUP BY symbol
ORDER BY total_pnl DESC;


-- 3. P/L by calendar year and term
-- ------------------------------------------------------------
SELECT
    date_part('year', sell_date)::int               AS year,
    term,
    count(*)                                        AS lots,
    round(sum(realized_pnl)::numeric, 2)            AS total_pnl
FROM realized_gains
GROUP BY year, term
ORDER BY year DESC, term;


-- 4. All lots — full detail
-- ------------------------------------------------------------
SELECT
    symbol,
    buy_date,
    sell_date,
    quantity,
    round(buy_price::numeric, 4)                    AS buy_px,
    round(sell_price::numeric, 4)                   AS sell_px,
    round(cost_basis::numeric, 2)                   AS cost_basis,
    round(proceeds::numeric, 2)                     AS proceeds,
    round(realized_pnl::numeric, 2)                 AS pnl,
    holding_days,
    term
FROM realized_gains
ORDER BY sell_date DESC, symbol;


-- 5. Win/loss trade statistics
-- (a "trade" = one FIFO lot; partial fills from the same sell are separate lots)
-- ------------------------------------------------------------
SELECT
    count(*)                                        AS total_lots,
    count(*) FILTER (WHERE realized_pnl > 0)        AS winners,
    count(*) FILTER (WHERE realized_pnl < 0)        AS losers,
    count(*) FILTER (WHERE realized_pnl = 0)        AS breakeven,
    round(
        count(*) FILTER (WHERE realized_pnl > 0)::numeric
        / nullif(count(*), 0) * 100
    , 1)                                            AS win_rate_pct,
    round(avg(realized_pnl) FILTER (WHERE realized_pnl > 0)::numeric, 2)   AS avg_win,
    round(avg(realized_pnl) FILTER (WHERE realized_pnl < 0)::numeric, 2)   AS avg_loss,
    round(sum(realized_pnl)::numeric, 2)            AS total_pnl
FROM realized_gains;
