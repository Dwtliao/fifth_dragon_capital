-- ============================================================
-- Pipeline monitoring queries
-- Sources: sync_log, reconciliation_log, sync_state
-- Run: psql $DATABASE_URL -f pipeline.sql
-- ============================================================


-- 1. Recent pipeline runs (last 20)
-- ------------------------------------------------------------
SELECT
    job_name,
    status,
    started_at::timestamptz AT TIME ZONE 'America/New_York'  AS started_et,
    round(duration_s::numeric, 1)                            AS duration_s,
    triggered_by,
    error_msg
FROM sync_log
ORDER BY started_at DESC
LIMIT 20;


-- 2. Run success rate by job
-- ------------------------------------------------------------
SELECT
    job_name,
    count(*)                                                AS total_runs,
    count(*) FILTER (WHERE status = 'success')              AS success,
    count(*) FILTER (WHERE status = 'failed')               AS failed,
    round(
        count(*) FILTER (WHERE status = 'success')::numeric
        / nullif(count(*), 0) * 100
    , 1)                                                    AS success_rate_pct,
    round(avg(duration_s) FILTER (WHERE status = 'success')::numeric, 1) AS avg_duration_s
FROM sync_log
GROUP BY job_name
ORDER BY job_name;


-- 3. Latest sync watermarks per account and data type
-- ------------------------------------------------------------
SELECT
    account_id_key,
    data_type,
    last_synced_at::timestamptz AT TIME ZONE 'America/New_York'  AS last_synced_et
FROM sync_state
ORDER BY account_id_key, data_type;


-- 4. Reconciliation summary (latest run)
-- ------------------------------------------------------------
SELECT
    status,
    count(*)                                        AS positions,
    string_agg(symbol, ', ' ORDER BY symbol)        AS symbols
FROM reconciliation_log
WHERE run_at = (SELECT MAX(run_at) FROM reconciliation_log)
GROUP BY status
ORDER BY status;


-- 5. Reconciliation discrepancies — detail (latest run)
-- ------------------------------------------------------------
SELECT
    account_id_key,
    symbol,
    round(ledger_qty::numeric, 4)                   AS ledger_qty,
    round(api_qty::numeric, 4)                      AS api_qty,
    round(delta::numeric, 4)                        AS delta,
    status,
    note
FROM reconciliation_log
WHERE run_at = (SELECT MAX(run_at) FROM reconciliation_log)
  AND status NOT IN ('match', 'ledger_only')
ORDER BY status, symbol;


-- 6. Row counts across all tables
-- ------------------------------------------------------------
SELECT 'accounts'           AS tbl, count(*) AS rows FROM accounts
UNION ALL SELECT 'balances',          count(*) FROM balances
UNION ALL SELECT 'positions',         count(*) FROM positions
UNION ALL SELECT 'transactions',      count(*) FROM transactions
UNION ALL SELECT 'orders',            count(*) FROM orders
UNION ALL SELECT 'order_details',     count(*) FROM order_details
UNION ALL SELECT 'ledger',            count(*) FROM ledger
UNION ALL SELECT 'realized_gains',    count(*) FROM realized_gains
UNION ALL SELECT 'sync_log',          count(*) FROM sync_log
UNION ALL SELECT 'reconciliation_log', count(*) FROM reconciliation_log
ORDER BY tbl;
