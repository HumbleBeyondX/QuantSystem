-- System audit log for risk events, circuit breakers, and system health
CREATE TABLE IF NOT EXISTS audit_log (
    timestamp   TIMESTAMPTZ NOT NULL,
    event_type  TEXT        NOT NULL,  -- 'risk_alert', 'circuit_breaker', 'system_health', 'trade_execution'
    severity    TEXT        NOT NULL DEFAULT 'INFO',  -- 'INFO', 'WARNING', 'CRITICAL'
    source      TEXT        DEFAULT '',
    strategy_id TEXT        DEFAULT '',
    message     TEXT        NOT NULL,
    details     JSONB       DEFAULT '{}'
);

SELECT create_hypertable('audit_log', 'timestamp', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_log (event_type, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_severity ON audit_log (severity, timestamp DESC);

-- Retention policy: keep audit logs for 1 year
SELECT add_retention_policy('audit_log', INTERVAL '365 days', if_not_exists => TRUE);
