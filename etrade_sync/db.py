import psycopg2
from etrade_sync.config import DATABASE_URL


def get_connection():
    return psycopg2.connect(DATABASE_URL)


def create_tables():
    sql = """
    CREATE TABLE IF NOT EXISTS accounts (
        id               SERIAL PRIMARY KEY,
        account_id_key   TEXT UNIQUE NOT NULL,
        account_id       TEXT,
        account_name     TEXT,
        account_desc     TEXT,
        account_mode     TEXT,
        account_type     TEXT,
        institution_type TEXT,
        status           TEXT,
        closed_date      TIMESTAMPTZ,
        raw              JSONB,
        created_at       TIMESTAMPTZ DEFAULT NOW(),
        updated_at       TIMESTAMPTZ DEFAULT NOW()
    );

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

    CREATE TABLE IF NOT EXISTS positions (
        id               SERIAL PRIMARY KEY,
        account_id_key   TEXT NOT NULL,
        fetched_at       TIMESTAMPTZ DEFAULT NOW(),
        position_id      BIGINT,
        symbol           TEXT,
        symbol_desc      TEXT,
        security_type    TEXT,
        position_type    TEXT,
        quantity         NUMERIC,
        cost_per_share   NUMERIC,
        total_cost       NUMERIC,
        market_value     NUMERIC,
        total_gain       NUMERIC,
        total_gain_pct   NUMERIC,
        days_gain        NUMERIC,
        days_gain_pct    NUMERIC,
        pct_of_portfolio NUMERIC,
        raw              JSONB
    );

    CREATE TABLE IF NOT EXISTS transactions (
        id               SERIAL PRIMARY KEY,
        account_id_key   TEXT NOT NULL,
        transaction_id   TEXT UNIQUE NOT NULL,
        transaction_date TIMESTAMPTZ,
        transaction_type TEXT,
        description      TEXT,
        description2     TEXT,
        amount           NUMERIC,
        symbol           TEXT,
        quantity         NUMERIC,
        price            NUMERIC,
        fee              NUMERIC,
        settlement_date  TIMESTAMPTZ,
        raw              JSONB,
        created_at       TIMESTAMPTZ DEFAULT NOW()
    );

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

    CREATE TABLE IF NOT EXISTS order_details (
        id                   SERIAL PRIMARY KEY,
        order_id             BIGINT NOT NULL REFERENCES orders(order_id),
        order_number         INT,
        symbol               TEXT,
        symbol_desc          TEXT,
        security_type        TEXT,
        order_action         TEXT,
        price_type           TEXT,
        order_term           TEXT,
        limit_price          NUMERIC,
        stop_price           NUMERIC,
        status               TEXT,
        placed_time          TIMESTAMPTZ,
        executed_time        TIMESTAMPTZ,
        ordered_quantity     NUMERIC,
        filled_quantity      NUMERIC,
        avg_execution_price  NUMERIC,
        estimated_commission NUMERIC,
        call_put             TEXT,
        expiry_year          INT,
        expiry_month         INT,
        expiry_day           INT,
        strike_price         NUMERIC,
        raw                  JSONB,
        created_at           TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS sync_state (
        id               SERIAL PRIMARY KEY,
        account_id_key   TEXT NOT NULL,
        data_type        TEXT NOT NULL,
        last_synced_at   TIMESTAMPTZ,
        last_marker      TEXT,
        UNIQUE (account_id_key, data_type)
    );
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
