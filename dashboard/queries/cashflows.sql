-- ============================================================
-- Cash flow queries
-- Sources: ledger (dividends, interest, deposits, withdrawals, fees)
-- Run: psql $DATABASE_URL -f cashflows.sql
-- ============================================================


-- 1. Income summary (dividends + interest) by year
-- ------------------------------------------------------------
SELECT
    date_part('year', event_timestamp)::int         AS year,
    event_type,
    count(*)                                        AS events,
    round(sum(net_amount)::numeric, 2)              AS total_amount
FROM ledger
WHERE event_type IN ('dividend', 'dividend_qualified', 'interest')
GROUP BY year, event_type
ORDER BY year DESC, event_type;


-- 2. Net cash flow by year (deposits, withdrawals, dividends, interest, fees)
-- ------------------------------------------------------------
SELECT
    date_part('year', event_timestamp)::int         AS year,
    round(sum(net_amount) FILTER (
        WHERE event_type IN ('deposit', 'transfer')
    )::numeric, 2)                                  AS deposits,
    round(sum(net_amount) FILTER (
        WHERE event_type = 'withdrawal'
    )::numeric, 2)                                  AS withdrawals,
    round(sum(net_amount) FILTER (
        WHERE event_type IN ('dividend', 'dividend_qualified')
    )::numeric, 2)                                  AS dividends,
    round(sum(net_amount) FILTER (
        WHERE event_type = 'interest'
    )::numeric, 2)                                  AS interest,
    round(sum(net_amount) FILTER (
        WHERE event_type = 'fee'
    )::numeric, 2)                                  AS fees,
    round(sum(net_amount)::numeric, 2)              AS net_total
FROM ledger
WHERE event_type IN (
    'deposit', 'withdrawal', 'transfer',
    'dividend', 'dividend_qualified', 'interest', 'fee'
)
GROUP BY year
ORDER BY year DESC;


-- 3. Dividend income by symbol (all time)
-- ------------------------------------------------------------
SELECT
    symbol,
    count(*)                                        AS payments,
    round(sum(net_amount)::numeric, 2)              AS total_income,
    min(event_timestamp)::date                      AS first_payment,
    max(event_timestamp)::date                      AS last_payment
FROM ledger
WHERE event_type IN ('dividend', 'dividend_qualified')
  AND symbol IS NOT NULL
GROUP BY symbol
ORDER BY total_income DESC;


-- 4. All cash events — full detail (most recent first)
-- ------------------------------------------------------------
SELECT
    event_timestamp::date                           AS date,
    event_type,
    symbol,
    round(net_amount::numeric, 2)                   AS amount,
    account_id_key
FROM ledger
WHERE event_type IN (
    'deposit', 'withdrawal', 'transfer',
    'dividend', 'dividend_qualified', 'interest', 'fee', 'redemption'
)
ORDER BY event_timestamp DESC;
