-- FIFO-matched realized gain/loss lots.
-- Populated by Python (etrade_sync.analytics.realized_pnl.build_realized_pnl).
-- Truncated and rebuilt after every ledger rebuild.
CREATE TABLE IF NOT EXISTS realized_gains (
    id              SERIAL PRIMARY KEY,
    account_id_key  TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    buy_ledger_id   INTEGER REFERENCES ledger(id),
    buy_date        DATE NOT NULL,
    buy_price       NUMERIC NOT NULL,
    sell_ledger_id  INTEGER REFERENCES ledger(id),
    sell_date       DATE NOT NULL,
    sell_price      NUMERIC NOT NULL,
    quantity        NUMERIC NOT NULL,       -- shares matched in this lot
    cost_basis      NUMERIC NOT NULL,       -- quantity × buy_price
    proceeds        NUMERIC NOT NULL,       -- quantity × sell_price
    realized_pnl    NUMERIC NOT NULL,       -- proceeds − cost_basis
    holding_days    INTEGER,
    term            TEXT CHECK (term IN ('short', 'long')),
    computed_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS realized_gains_account_symbol ON realized_gains (account_id_key, symbol);
CREATE INDEX IF NOT EXISTS realized_gains_sell_date      ON realized_gains (sell_date);
CREATE INDEX IF NOT EXISTS realized_gains_term           ON realized_gains (term);
