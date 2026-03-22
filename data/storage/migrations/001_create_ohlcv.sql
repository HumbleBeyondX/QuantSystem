-- OHLCV candlestick data (hypertable)
CREATE TABLE IF NOT EXISTS ohlcv (
    timestamp   TIMESTAMPTZ NOT NULL,
    symbol      TEXT        NOT NULL,
    asset_class TEXT        NOT NULL DEFAULT 'crypto',
    timeframe   TEXT        NOT NULL DEFAULT '1m',
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    volume      DOUBLE PRECISION NOT NULL DEFAULT 0,
    vwap        DOUBLE PRECISION,
    UNIQUE (timestamp, symbol, timeframe)
);

-- Convert to TimescaleDB hypertable (partitioned by time)
SELECT create_hypertable('ohlcv', 'timestamp', if_not_exists => TRUE);

-- Compression policy: compress chunks older than 7 days
ALTER TABLE ohlcv SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol,timeframe',
    timescaledb.compress_orderby = 'timestamp DESC'
);

SELECT add_compression_policy('ohlcv', INTERVAL '7 days', if_not_exists => TRUE);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_tf ON ohlcv (symbol, timeframe, timestamp DESC);
