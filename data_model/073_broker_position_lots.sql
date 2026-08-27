-- E*TRADE-reported current open tax lots. These are append-only snapshots and
-- intentionally separate from open_lots, which is a local FIFO reconstruction.
CREATE TABLE IF NOT EXISTS broker_position_lots (
    id                    BIGSERIAL PRIMARY KEY,
    account_id_key        TEXT NOT NULL,
    symbol                TEXT NOT NULL,
    position_id           BIGINT,
    position_lot_id       BIGINT NOT NULL,
    acquired_date         DATE,
    original_quantity     NUMERIC,
    remaining_quantity    NUMERIC NOT NULL,
    price                 NUMERIC,
    total_cost            NUMERIC,
    market_value          NUMERIC,
    total_gain            NUMERIC,
    position_fetched_at   TIMESTAMPTZ NOT NULL,
    raw                   JSONB NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (account_id_key, symbol, position_lot_id, position_fetched_at)
);

CREATE INDEX IF NOT EXISTS broker_position_lots_latest_idx
    ON broker_position_lots (account_id_key, symbol, position_fetched_at DESC);
