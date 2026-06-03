CREATE TABLE IF NOT EXISTS orders (
    id                SERIAL PRIMARY KEY,
    account_id_key    TEXT NOT NULL,
    order_id          BIGINT UNIQUE NOT NULL,
    order_type        TEXT,
    total_order_value NUMERIC,
    total_commission  NUMERIC,
    placed_time       TIMESTAMPTZ,
    status            TEXT,
    raw               JSONB,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);
