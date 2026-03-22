"""Pydantic schemas for API request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Portfolio ───────────────────────────────────────────────

class PositionResponse(BaseModel):
    symbol: str
    quantity: float
    avg_price: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    pnl_pct: float
    asset_class: str
    strategy_id: str = ""


class PortfolioSummaryResponse(BaseModel):
    total_value: float
    cash: float
    positions_value: float
    unrealized_pnl: float
    realized_pnl: float
    total_pnl_pct: float
    positions_count: int
    daily_pnl: float = 0.0


class EquityCurvePoint(BaseModel):
    timestamp: str
    equity: float
    drawdown: float = 0.0


# ── Strategies ──────────────────────────────────────────────

class StrategyResponse(BaseModel):
    id: str
    name: str
    asset_class: str
    state: str  # "running" | "paused" | "stopped"
    pnl: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    sharpe: float = 0.0


class SignalResponse(BaseModel):
    id: str
    strategy_id: str
    symbol: str
    direction: str
    strength: float
    timestamp: str


# ── Backtest ────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    strategy_id: str
    data_path: str = ""
    start_date: str = ""
    end_date: str = ""
    initial_capital: float = Field(default=100_000.0, ge=1000)
    parameters: Dict[str, Any] = Field(default_factory=dict)


class BacktestMetricsResponse(BaseModel):
    total_return: float
    annual_return: float
    sharpe: float
    sortino: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    total_trades: int
    calmar: float = 0.0


class BacktestResultResponse(BaseModel):
    id: str
    strategy_id: str
    start_date: str
    end_date: str
    metrics: BacktestMetricsResponse
    created_at: str = ""


# ── Risk ────────────────────────────────────────────────────

class RiskSummaryResponse(BaseModel):
    portfolio_var: float
    portfolio_cvar: float
    current_drawdown: float
    max_drawdown: float
    leverage: float
    daily_vol: float = 0.0
    sharpe_estimate: float = 0.0


class GreeksResponse(BaseModel):
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    rho: float = 0.0


# ── Market Data ─────────────────────────────────────────────

class OHLCVResponse(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class OrderBookLevelResponse(BaseModel):
    price: float
    quantity: float
    total: float = 0.0


class OrderBookResponse(BaseModel):
    bids: List[OrderBookLevelResponse]
    asks: List[OrderBookLevelResponse]
    spread: float = 0.0
    timestamp: str = ""


# ── System ──────────────────────────────────────────────────

class ServiceStatusResponse(BaseModel):
    name: str
    status: str  # "up" | "down"
    latency_ms: Optional[float] = None
    last_check: str = ""


class SystemHealthResponse(BaseModel):
    status: str  # "healthy" | "degraded" | "down"
    uptime_seconds: float = 0.0
    services: List[ServiceStatusResponse] = Field(default_factory=list)
