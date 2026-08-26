-- Latest broker-lot quantity coverage versus the matching E*TRADE aggregate
-- position snapshot. This deliberately does not compare individual broker lots
-- to local FIFO lots: specific-lot sales are expected to differ from FIFO.
CREATE OR REPLACE VIEW broker_lot_coverage AS
WITH latest_positions AS (
    SELECT
        p.account_id_key,
        p.symbol,
        SUM(p.quantity) AS position_quantity,
        MAX(p.fetched_at) AS position_fetched_at
    FROM positions p
    WHERE (p.account_id_key, p.fetched_at) IN (
        SELECT account_id_key, MAX(fetched_at)
        FROM positions
        GROUP BY account_id_key
    )
      AND p.security_type = 'EQ'
      AND p.symbol IS NOT NULL
    GROUP BY p.account_id_key, p.symbol
),
broker_totals AS (
    SELECT
        account_id_key,
        symbol,
        position_fetched_at,
        SUM(remaining_quantity) AS broker_lot_quantity,
        COUNT(*) AS broker_lot_count
    FROM broker_position_lots
    GROUP BY account_id_key, symbol, position_fetched_at
)
SELECT
    p.account_id_key,
    p.symbol,
    p.position_quantity,
    p.position_fetched_at,
    b.broker_lot_quantity,
    b.broker_lot_count,
    ABS(b.broker_lot_quantity - p.position_quantity) <= 0.01 AS quantity_complete
FROM latest_positions p
JOIN broker_totals b
  ON b.account_id_key = p.account_id_key
 AND b.symbol = p.symbol
 AND b.position_fetched_at = p.position_fetched_at;
