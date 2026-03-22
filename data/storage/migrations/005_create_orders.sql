-- Orders audit log (hypertable)
CREATE TABLE IF NOT EXISTS orders_log (
    timestamp          TIMESTAMPTZ NOT NULL,
    order_id           TEXT        NOT NULL,
    strategy_id        TEXT        NOT NULL,
    symbol             TEXT        NOT NULL,
    side               TEXT        NOT NULL,
    order_type         TEXT        NOT NULL,
    quantity           DOUBLE PRECISION NOT NULL,
    status             TEXT        NOT NULL,
    limit_price        DOUBLE PRECISION,
    filled_quantity    DOUBLE PRECISION DEFAULT 0,
    average_fill_price DOUBLE PRECISION DEFAULT 0,
    commission         DOUBLE PRECISION DEFAULT 0,
    broker_order_id    TEXT,
    metadata           JSONB       DEFAULT '{}'
);

SELECT create_hypertable('orders_log', 'timestamp', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_orders_strategy ON orders_log (strategy_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders_log (symbol, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders_log (status, timestamp DESC);
