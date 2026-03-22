"""Paper Trading monitoring API endpoints.

Reads live paper trading state from:
  - data/live_state/gamma_scalp_state.pkl  (strategy state)
  - logs/paper_trading/gamma_scalp_daily.jsonl  (daily snapshots)
  - logs/paper_trading/gamma_scalp_trades.jsonl  (trade log)

Provides endpoints for the dashboard to monitor paper/live trading in real-time.
"""

from __future__ import annotations

import json
import os
import pickle
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel

router = APIRouter()

# Project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

STATE_FILE = PROJECT_ROOT / "data" / "live_state" / "gamma_scalp_state.pkl"
DAILY_LOG = PROJECT_ROOT / "logs" / "paper_trading" / "gamma_scalp_daily.jsonl"
TRADE_LOG = PROJECT_ROOT / "logs" / "paper_trading" / "gamma_scalp_trades.jsonl"
ENV_FILE = PROJECT_ROOT / ".env"


# ── Schemas ───────────────────────────────────────────────────

class TradingStatus(BaseModel):
    """Current paper trading status."""
    is_running: bool = False
    data_source: str = "yahoo"
    execution_mode: str = "internal"
    last_bar_date: Optional[str] = None
    spy_price: float = 0.0
    vix: float = 0.0
    implied_vol: float = 0.0
    rv_20d: float = 0.0
    vrp: float = 0.0

    # Position
    position_open: bool = False
    trade_direction: str = "none"
    contracts: int = 0
    strike: float = 0.0
    hedge_shares: float = 0.0

    # P&L
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_equity: float = 100000.0
    initial_capital: float = 100000.0

    # Greeks
    portfolio_delta: float = 0.0
    portfolio_gamma: float = 0.0
    portfolio_theta: float = 0.0
    portfolio_vega: float = 0.0

    # Risk
    total_trades: int = 0
    consecutive_losses: int = 0
    peak_equity: float = 0.0

    # Broker
    broker_connected: bool = False
    broker_equity: Optional[float] = None
    broker_cash: Optional[float] = None


class DailySnapshot(BaseModel):
    date: str
    spy_close: float = 0.0
    vix: float = 0.0
    implied_vol: float = 0.0
    rv_20d: float = 0.0
    actions: List[str] = []
    position_open: bool = False
    trade_direction: str = "none"
    contracts: int = 0
    strike: float = 0.0
    hedge_shares: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_equity: float = 100000.0
    total_trades: int = 0
    portfolio_delta: float = 0.0
    portfolio_gamma: float = 0.0
    portfolio_theta: float = 0.0


class TradeRecord(BaseModel):
    timestamp: str = ""
    direction: str = ""
    reason: str = ""
    entry_spot: float = 0.0
    exit_spot: float = 0.0
    strike: float = 0.0
    entry_iv: float = 0.0
    exit_iv: float = 0.0
    contracts: int = 0
    total_pnl: float = 0.0
    option_pnl: float = 0.0
    hedge_pnl: float = 0.0
    gamma_pnl: float = 0.0
    theta_pnl: float = 0.0
    vega_pnl: float = 0.0
    txn_costs: float = 0.0
    running_total: float = 0.0


class EquityPoint(BaseModel):
    date: str
    equity: float
    drawdown: float = 0.0


class SignalDetail(BaseModel):
    date: str
    regime: str = "neutral"
    composite: float = 0.0
    vrp_signal: float = 0.0
    vix_mr_signal: float = 0.0
    vol_mom_signal: float = 0.0
    vov_factor: float = 0.0
    action: str = "NO_ACTION"


class RunConfig(BaseModel):
    data_source: str = "alpaca"
    execution: bool = True
    initial_capital: float = 100000.0


# ── Helper: load state ────────────────────────────────────────

def _load_state() -> Optional[Dict[str, Any]]:
    """Load strategy state from pickle file."""
    if not STATE_FILE.exists():
        return None
    try:
        with open(STATE_FILE, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _load_jsonl(filepath: Path, limit: int = 500) -> List[Dict[str, Any]]:
    """Load last N lines from a JSONL file."""
    if not filepath.exists():
        return []
    lines = []
    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        lines.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return lines[-limit:]
    except Exception:
        return []


def _check_broker_status() -> Dict[str, Any]:
    """Check if Alpaca broker is accessible."""
    result = {"connected": False, "equity": None, "cash": None}
    try:
        # Load .env
        env = {}
        if ENV_FILE.exists():
            with open(ENV_FILE) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip()

        api_key = env.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY")
        secret_key = env.get("ALPACA_SECRET_KEY") or os.environ.get("ALPACA_SECRET_KEY")

        if not api_key or not secret_key:
            return result

        from alpaca.trading.client import TradingClient
        tc = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
        account = tc.get_account()
        result["connected"] = account.status == "ACTIVE"
        result["equity"] = float(account.equity)
        result["cash"] = float(account.cash)
    except Exception:
        pass
    return result


# ── Endpoints ─────────────────────────────────────────────────

@router.get("/status", response_model=TradingStatus)
async def get_trading_status():
    """Get current paper trading status and strategy state."""
    state = _load_state()

    if state is None:
        return TradingStatus()

    # Compute unrealized P&L
    unrealized = 0.0
    if state.get("_straddle_open") and state.get("_n_contracts", 0) > 0:
        try:
            sys.path.insert(0, str(PROJECT_ROOT))
            from strategies.options.gamma_scalp_strategy import bs_straddle
            spot = state.get("_last_spot", 0)
            strike = state.get("_strike", 0)
            iv = state.get("_implied_vol", 0.2)
            rf = state.get("_risk_free", 0.045)
            expiry = state.get("_expiry_time")
            current = state.get("_current_time", datetime.now())
            if expiry and current:
                T = max((expiry - current).total_seconds(), 60) / (365.25 * 86400)
            else:
                T = 5 / 365.0
            cp, pp = bs_straddle(spot, strike, T, rf, iv)
            n = state.get("_n_contracts", 0) * 100
            direction = state.get("_trade_direction", "neutral")
            sign = -1.0 if direction == "sell_vol" else 1.0
            entry_c = state.get("_entry_call_price", 0)
            entry_p = state.get("_entry_put_price", 0)
            unrealized = ((cp + pp) - (entry_c + entry_p)) * n * sign
            unrealized += state.get("_cum_hedge_pnl", 0) - state.get("_cum_txn_costs", 0)
        except Exception:
            pass

    initial_capital = state.get("_initial_capital", 100000)
    running_pnl = state.get("_running_pnl", 0)
    equity = initial_capital + running_pnl + unrealized

    # Check broker
    broker = _check_broker_status()

    current_time = state.get("_current_time")
    last_date = None
    if current_time:
        if hasattr(current_time, "date"):
            last_date = str(current_time.date())
        else:
            last_date = str(current_time)

    iv = state.get("_implied_vol", 0.2)
    rv20 = state.get("_rv_20d", 0)

    return TradingStatus(
        is_running=STATE_FILE.exists(),
        last_bar_date=last_date,
        spy_price=round(state.get("_last_spot", 0), 2),
        vix=round(iv * 100, 2),
        implied_vol=round(iv, 4),
        rv_20d=round(rv20, 4),
        vrp=round(iv - rv20, 4),
        position_open=state.get("_straddle_open", False),
        trade_direction=state.get("_trade_direction", "none") if state.get("_straddle_open") else "none",
        contracts=state.get("_n_contracts", 0) if state.get("_straddle_open") else 0,
        strike=state.get("_strike", 0) if state.get("_straddle_open") else 0,
        hedge_shares=round(state.get("_hedge_shares", 0), 0) if state.get("_straddle_open") else 0,
        realized_pnl=round(running_pnl, 2),
        unrealized_pnl=round(unrealized, 2),
        total_equity=round(equity, 2),
        initial_capital=initial_capital,
        portfolio_delta=round(state.get("_portfolio_delta", 0), 2) if state.get("_straddle_open") else 0,
        portfolio_gamma=round(state.get("_portfolio_gamma", 0), 2) if state.get("_straddle_open") else 0,
        portfolio_theta=round(state.get("_portfolio_theta", 0), 2) if state.get("_straddle_open") else 0,
        portfolio_vega=round(state.get("_portfolio_vega", 0), 2) if state.get("_straddle_open") else 0,
        total_trades=len(state.get("_trade_results", [])),
        consecutive_losses=state.get("_consecutive_losses", 0),
        peak_equity=round(state.get("_peak_equity", 0), 2),
        broker_connected=broker["connected"],
        broker_equity=broker.get("equity"),
        broker_cash=broker.get("cash"),
    )


@router.get("/daily-log", response_model=List[DailySnapshot])
async def get_daily_log(limit: int = Query(default=100, ge=1, le=1000)):
    """Get daily paper trading snapshots."""
    entries = _load_jsonl(DAILY_LOG, limit)
    return [DailySnapshot(**e) for e in entries]


@router.get("/trades", response_model=List[TradeRecord])
async def get_trades(limit: int = Query(default=100, ge=1, le=500)):
    """Get paper trading trade history."""
    # First try from state (more complete)
    state = _load_state()
    if state and state.get("_trade_results"):
        results = state["_trade_results"][-limit:]
        return [TradeRecord(**t) for t in results]

    # Fallback to JSONL
    entries = _load_jsonl(TRADE_LOG, limit)
    return [TradeRecord(**e) for e in entries]


@router.get("/equity-curve", response_model=List[EquityPoint])
async def get_equity_curve():
    """Get equity curve from daily log."""
    entries = _load_jsonl(DAILY_LOG, 1000)
    if not entries:
        return []

    peak = 0.0
    result = []
    for e in entries:
        equity = e.get("total_equity", 100000)
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0
        result.append(EquityPoint(
            date=e.get("date", ""),
            equity=round(equity, 2),
            drawdown=round(dd * 100, 2),
        ))
    return result


@router.get("/signal-history", response_model=List[SignalDetail])
async def get_signal_history(limit: int = Query(default=50, ge=1, le=200)):
    """Get signal computation history from daily log."""
    entries = _load_jsonl(DAILY_LOG, limit)
    result = []
    for e in entries:
        result.append(SignalDetail(
            date=e.get("date", ""),
            regime=e.get("trade_direction", "neutral") if e.get("position_open") else "neutral",
            composite=0.0,  # Not stored in daily log
            vrp_signal=round(e.get("implied_vol", 0) - e.get("rv_20d", 0), 4),
            action=", ".join(e.get("actions", ["NO_ACTION"])),
        ))
    return result


@router.post("/run-once")
async def run_paper_trading_once(
    background_tasks: BackgroundTasks,
    config: RunConfig = RunConfig(),
):
    """Trigger a single paper trading iteration."""

    def _run():
        python = sys.executable
        script = str(PROJECT_ROOT / "scripts" / "live_paper_trade.py")
        cmd = [
            python, script,
            "--data-source", config.data_source,
            "--mode", "once",
            "--initial-capital", str(config.initial_capital),
        ]
        if config.execution and config.data_source == "alpaca":
            cmd.append("--alpaca-execution")
        elif config.execution and config.data_source == "ib":
            cmd.append("--ib-execution")

        subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    background_tasks.add_task(_run)
    return {"status": "started", "message": "Paper trading iteration started in background"}


@router.post("/reset")
async def reset_state():
    """Reset paper trading state (start fresh)."""
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    return {"status": "reset", "message": "State cleared. Next run will start fresh with warm-up."}


@router.get("/config")
async def get_config():
    """Get available trading configurations."""
    # Check what data sources are available
    sources = ["yahoo"]  # Always available

    # Check Alpaca
    try:
        env = {}
        if ENV_FILE.exists():
            with open(ENV_FILE) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env[k.strip()] = v.strip()
        if env.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY"):
            sources.append("alpaca")
    except Exception:
        pass

    # IB always available if ib_insync installed
    try:
        import ib_insync
        sources.append("ib")
    except ImportError:
        pass

    return {
        "available_sources": sources,
        "strategies": [
            {
                "id": "spy_gamma_scalp",
                "name": "SPY Gamma Scalp",
                "description": "Volatility risk premium exploitation via ATM straddle selling + delta hedging",
                "asset_class": "options",
                "sharpe": 1.98,
                "backtest_trades": 40,
                "backtest_win_rate": 0.80,
            }
        ],
        "execution_modes": ["internal", "alpaca", "ib"],
        "state_exists": STATE_FILE.exists(),
    }
