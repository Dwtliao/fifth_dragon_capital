CREATE TABLE IF NOT EXISTS balances (
    id                          SERIAL PRIMARY KEY,
    account_id_key              TEXT NOT NULL,
    fetched_at                  TIMESTAMPTZ DEFAULT NOW(),
    cash_available_for_invest   NUMERIC,
    cash_available_for_withdraw NUMERIC,
    net_cash                    NUMERIC,
    cash_balance                NUMERIC,
    total_account_value         NUMERIC,
    net_mv                      NUMERIC,
    net_mv_long                 NUMERIC,
    raw                         JSONB
);
