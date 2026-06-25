-- Audit log for every market sell order attempt (preview + place).
-- One row per clientOrderId. Never deleted — permanent record of all order activity.
CREATE TABLE IF NOT EXISTS sell_order_audit (
    id                   SERIAL PRIMARY KEY,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    account_id_key       TEXT        NOT NULL,
    symbol               TEXT        NOT NULL,
    quantity             NUMERIC     NOT NULL,
    client_order_id      TEXT        NOT NULL UNIQUE,
    preview_id           TEXT,
    estimated_price      NUMERIC,
    estimated_total      NUMERIC,
    estimated_commission NUMERIC,
    preview_response     JSONB,
    place_response       JSONB,
    status               TEXT        NOT NULL DEFAULT 'previewed',  -- previewed | placed | failed
    error_msg            TEXT
);
