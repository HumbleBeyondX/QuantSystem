-- Computed feature values (hypertable)
CREATE TABLE IF NOT EXISTS features (
    timestamp     TIMESTAMPTZ      NOT NULL,
    symbol        TEXT             NOT NULL,
    feature_name  TEXT             NOT NULL,
    feature_value DOUBLE PRECISION NOT NULL,
    UNIQUE (timestamp, symbol, feature_name)
);

SELECT create_hypertable('features', 'timestamp', if_not_exists => TRUE);

ALTER TABLE features SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol,feature_name',
    timescaledb.compress_orderby = 'timestamp DESC'
);

SELECT add_compression_policy('features', INTERVAL '7 days', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_features_symbol_name ON features (symbol, feature_name, timestamp DESC);
