-- Raw trade data (hypertable)
CREATE TABLE IF NOT EXISTS trades (
    timestamp   TIMESTAMPTZ      NOT NULL,
    symbol      TEXT             NOT NULL,
    price       DOUBLE PRECISION NOT NULL,
    amount      DOUBLE PRECISION NOT NULL,
    side        TEXT             NOT NULL,  -- 'BUY' or 'SELL'
    source      TEXT             DEFAULT '',
    trade_id    TEXT             DEFAULT ''
);

SELECT create_hypertable('trades', 'timestamp', if_not_exists => TRUE);

ALTER TABLE trades SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby = 'timestamp DESC'
);

SELECT add_compression_policy('trades', INTERVAL '3 days', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol, timestamp DESC);
