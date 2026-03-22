-- Position snapshots (hypertable)
CREATE TABLE IF NOT EXISTS positions (
    timestamp          TIMESTAMPTZ NOT NULL,
    strategy_id        TEXT        NOT NULL,
    symbol             TEXT        NOT NULL,
    side               TEXT        NOT NULL,  -- 'LONG', 'SHORT', 'FLAT'
    quantity           DOUBLE PRECISION NOT NULL,
    average_entry_price DOUBLE PRECISION NOT NULL,
    current_price      DOUBLE PRECISION DEFAULT 0,
    unrealized_pnl     DOUBLE PRECISION DEFAULT 0,
    realized_pnl       DOUBLE PRECISION DEFAULT 0,
    asset_class        TEXT        DEFAULT 'crypto'
);

SELECT create_hypertable('positions', 'timestamp', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_positions_strategy ON positions (strategy_id, symbol, timestamp DESC);

-- Portfolio snapshots
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    timestamp       TIMESTAMPTZ      NOT NULL,
    total_value     DOUBLE PRECISION NOT NULL,
    cash            DOUBLE PRECISION NOT NULL,
    invested_value  DOUBLE PRECISION DEFAULT 0,
    unrealized_pnl  DOUBLE PRECISION DEFAULT 0,
    realized_pnl    DOUBLE PRECISION DEFAULT 0,
    daily_pnl       DOUBLE PRECISION DEFAULT 0,
    leverage        DOUBLE PRECISION DEFAULT 0
);

SELECT create_hypertable('portfolio_snapshots', 'timestamp', if_not_exists => TRUE);
