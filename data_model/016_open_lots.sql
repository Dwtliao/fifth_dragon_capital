-- Open (unrealized) buy lots remaining after FIFO matching.
-- Truncated and rebuilt by build_realized_pnl() alongside realized_gains.
-- Prices and quantities are split-adjusted so they are in the same units
-- as current market prices.
CREATE TABLE IF NOT EXISTS open_lots (
    id               SERIAL PRIMARY KEY,
    account_id_key   TEXT    NOT NULL,
    symbol           TEXT    NOT NULL,
    buy_ledger_id    INTEGER REFERENCES ledger(id),
    buy_date         DATE    NOT NULL,
    buy_price        FLOAT   NOT NULL,  -- split-adjusted cost per share
    quantity         FLOAT   NOT NULL,  -- split-adjusted remaining shares
    cost_basis       FLOAT   NOT NULL,  -- buy_price * quantity
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS open_lots_account_symbol ON open_lots (account_id_key, symbol);
