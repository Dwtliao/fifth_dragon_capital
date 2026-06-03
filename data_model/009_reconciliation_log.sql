-- Stores results of each reconciliation run.
-- One row per (account, symbol) comparison between ledger and API positions.
CREATE TABLE IF NOT EXISTS reconciliation_log (
    id              SERIAL PRIMARY KEY,
    run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    account_id_key  TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    ledger_qty      NUMERIC,    -- reconstructed from ledger SUM(quantity)
    api_qty         NUMERIC,    -- from latest positions snapshot
    delta           NUMERIC,    -- ledger_qty - api_qty
    status          TEXT NOT NULL,  -- match | discrepancy | ledger_only | api_only
    note            TEXT            -- explanation (e.g. history gap, pre-2yr sells)
);

CREATE INDEX IF NOT EXISTS recon_log_run  ON reconciliation_log (run_at DESC);
CREATE INDEX IF NOT EXISTS recon_log_sym  ON reconciliation_log (symbol);
CREATE INDEX IF NOT EXISTS recon_log_status ON reconciliation_log (status);
