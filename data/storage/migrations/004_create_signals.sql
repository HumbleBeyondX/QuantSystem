-- Strategy signals log (hypertable)
CREATE TABLE IF NOT EXISTS signals (
    timestamp      TIMESTAMPTZ NOT NULL,
    signal_id      TEXT        NOT NULL,
    strategy_id    TEXT        NOT NULL,
    symbol         TEXT        NOT NULL,
    direction      INTEGER     NOT NULL,  -- 1=long, -1=short, 0=flat
    strength       DOUBLE PRECISION DEFAULT 0,
    target_quantity DOUBLE PRECISION,
    stop_loss      DOUBLE PRECISION,
    take_profit    DOUBLE PRECISION,
    metadata       JSONB       DEFAULT '{}'
);

SELECT create_hypertable('signals', 'timestamp', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals (strategy_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals (symbol, timestamp DESC);
