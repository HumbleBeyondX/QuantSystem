"""SQLAlchemy 2.0 ORM models for QuantSystem persistent storage.

Maps domain concepts (orders, fills, positions, snapshots) to relational tables.
Designed to work with both SQLite (local) and PostgreSQL (cloud via Supabase).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
    pass


# ------------------------------------------------------------------
# Orders
# ------------------------------------------------------------------

class OrderRecord(Base):
    """Persisted order — every order submitted through the OMS."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, default=_new_uuid)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)         # BUY / SELL
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)  # MARKET / LIMIT / ...
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    limit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stop_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    filled_quantity: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    average_fill_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    commission: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    broker_order_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False, default="crypto")
    exchange: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    leverage: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    margin_type: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)  # cross / isolated
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index("ix_orders_strategy_created", "strategy_id", "created_at"),
    )


# ------------------------------------------------------------------
# Fills
# ------------------------------------------------------------------

class FillRecord(Base):
    """Persisted fill — every execution event from broker/exchange."""

    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fill_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, default=_new_uuid)
    order_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    commission: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False, default="crypto")
    exchange: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    trade_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)  # exchange trade ID
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_fills_strategy_ts", "strategy_id", "timestamp"),
    )


# ------------------------------------------------------------------
# Position Snapshots
# ------------------------------------------------------------------

class PositionSnapshot(Base):
    """Point-in-time snapshot of a single position."""

    __tablename__ = "position_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # LONG / SHORT / FLAT
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    average_entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    current_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False, default="crypto")
    exchange: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    leverage: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_pos_snap_strategy_ts", "strategy_id", "timestamp"),
    )


# ------------------------------------------------------------------
# Portfolio Snapshots
# ------------------------------------------------------------------

class PortfolioSnapshotRecord(Base):
    """Aggregated portfolio state at a point in time."""

    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    total_value: Mapped[float] = mapped_column(Float, nullable=False)
    cash: Mapped[float] = mapped_column(Float, nullable=False)
    invested_value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    daily_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    leverage: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    num_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    positions_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON array of position details
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)


# ------------------------------------------------------------------
# Strategy State
# ------------------------------------------------------------------

class StrategyStateRecord(Base):
    """Persisted strategy lifecycle state — supports orchestrator recovery."""

    __tablename__ = "strategy_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="IDLE")  # IDLE/RUNNING/PAUSED/STOPPED/ERROR
    engine_type: Mapped[str] = mapped_column(String(16), nullable=False, default="paper")  # paper/ib/ccxt
    exchange: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # binance/bybit/okx
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False, default="crypto")
    symbols_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    config_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    allocation: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_heartbeat: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


# ------------------------------------------------------------------
# Risk Events
# ------------------------------------------------------------------

class RiskEventRecord(Base):
    """Logged risk event — pre-trade rejection, realtime alert, circuit breaker trigger."""

    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, default=_new_uuid)
    level: Mapped[str] = mapped_column(String(16), nullable=False)  # WARNING / CRITICAL / KILL
    source: Mapped[str] = mapped_column(String(32), nullable=False)  # pre_trade / realtime / circuit_breaker
    strategy_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    metric_name: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    metric_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    threshold: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)


# ------------------------------------------------------------------
# Audit Log
# ------------------------------------------------------------------

class AuditLogRecord(Base):
    """Immutable audit trail for all state-changing actions."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)  # order / position / strategy / ...
    entity_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    user: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    details_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)


# ------------------------------------------------------------------
# Backtest Results
# ------------------------------------------------------------------

class BacktestResultRecord(Base):
    """Persisted backtest result — replaces JSON file storage."""

    __tablename__ = "backtest_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    backtest_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    strategy_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, default="1d")
    start_date: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    end_date: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Metrics
    total_return: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sharpe_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sortino_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    calmar_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_drawdown_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    win_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profit_factor: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    total_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    annual_volatility: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Blobs
    trades_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    equity_curve_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    config_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        Index("ix_bt_strategy_created", "strategy_id", "created_at"),
    )


# ------------------------------------------------------------------
# Sync Tracking (for cloud bidirectional sync)
# ------------------------------------------------------------------

class SyncCursor(Base):
    """Tracks last sync timestamp per table for incremental cloud sync."""

    __tablename__ = "sync_cursors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    direction: Mapped[str] = mapped_column(String(8), nullable=False, default="up")  # up / down / both
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
