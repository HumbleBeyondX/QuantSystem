"""System health, overview, and infrastructure endpoints."""

from __future__ import annotations

import json
import os
import pickle
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
STATE_DIR = PROJECT_ROOT / "data" / "live_state"
LOG_DIR = PROJECT_ROOT / "logs" / "paper_trading"
DEPLOY_FILE = PROJECT_ROOT / "data" / "strategy_deployments.json"
ENV_FILE = PROJECT_ROOT / ".env"


class PortfolioOverview(BaseModel):
    """Aggregated portfolio metrics across all deployed strategies."""
    trading_mode: str = "paper"  # "paper" | "live" | "mixed"
    total_equity: float = 0.0
    initial_capital: float = 0.0
    total_return_pct: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    total_strategies: int = 0
    running_strategies: int = 0
    stopped_strategies: int = 0
    total_trades: int = 0
    win_rate: Optional[float] = None
    broker_connected: bool = False
    broker_equity: Optional[float] = None
    last_updated: str = ""


def _load_deployments() -> List[Dict[str, Any]]:
    if not DEPLOY_FILE.exists():
        return []
    try:
        with open(DEPLOY_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _load_state(filename: str) -> Optional[Dict[str, Any]]:
    state_file = STATE_DIR / filename
    if not state_file.exists():
        return None
    try:
        with open(state_file, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _load_daily_log(filename: str) -> List[Dict]:
    log_file = LOG_DIR / filename
    if not log_file.exists():
        return []
    lines = []
    try:
        with open(log_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        lines.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return lines
    except Exception:
        return []


def _check_broker() -> Dict[str, Any]:
    result = {"connected": False, "equity": None}
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    api_key = os.getenv("ALPACA_API_KEY", "")
    secret_key = os.getenv("ALPACA_SECRET_KEY", "")
    if not api_key or not secret_key:
        return result
    try:
        from alpaca.trading.client import TradingClient
        tc = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)
        account = tc.get_account()
        result["connected"] = account.status == "ACTIVE"
        result["equity"] = float(account.equity)
    except Exception:
        pass
    return result


STATE_MAP = {
    "spy_gamma_scalp": "gamma_scalp_state.pkl",
}
LOG_MAP = {
    "spy_gamma_scalp": "gamma_scalp_daily.jsonl",
}


@router.get("/overview", response_model=PortfolioOverview)
async def get_portfolio_overview():
    """Get aggregated portfolio overview across all deployed strategies."""
    deployments = _load_deployments()

    total_equity = 0.0
    total_initial = 0.0
    total_realized = 0.0
    total_trades = 0
    total_wins = 0
    running = 0
    stopped = 0
    modes = set()

    for dep in deployments:
        strategy_id = dep.get("strategy_id", "")
        mode = dep.get("mode", "paper")
        status = dep.get("status", "stopped")
        modes.add(mode)

        if status == "running":
            running += 1
        else:
            stopped += 1

        initial = dep.get("initial_capital", 100000)
        total_initial += initial

        # Try to get real state
        state_file = STATE_MAP.get(strategy_id)
        if state_file:
            state = _load_state(state_file)
            if state:
                pnl = state.get("_running_pnl", 0)
                total_realized += pnl
                total_equity += initial + pnl
                results = state.get("_trade_results", [])
                total_trades += len(results)
                total_wins += sum(1 for r in results if r.get("total_pnl", 0) > 0)
                continue

        total_equity += initial

    # Calculate max drawdown from daily logs
    max_dd = 0.0
    for strategy_id, log_file in LOG_MAP.items():
        entries = _load_daily_log(log_file)
        if entries:
            peak = 0.0
            for e in entries:
                eq = e.get("total_equity", 100000)
                peak = max(peak, eq)
                dd = (peak - eq) / peak if peak > 0 else 0
                max_dd = max(max_dd, dd)

    # If no deployments, still provide base data
    if not deployments:
        total_initial = 100000
        total_equity = 100000

    # Determine trading mode
    if len(modes) == 0:
        trading_mode = "paper"
    elif len(modes) == 1:
        trading_mode = modes.pop()
    else:
        trading_mode = "mixed"

    total_return = ((total_equity - total_initial) / total_initial * 100) if total_initial > 0 else 0
    win_rate = round(total_wins / total_trades * 100, 1) if total_trades > 0 else None

    broker = _check_broker()

    return PortfolioOverview(
        trading_mode=trading_mode,
        total_equity=round(total_equity, 2),
        initial_capital=round(total_initial, 2),
        total_return_pct=round(total_return, 2),
        realized_pnl=round(total_realized, 2),
        unrealized_pnl=0.0,
        max_drawdown_pct=round(max_dd * 100, 2),
        sharpe_ratio=0.0,  # Would need daily returns series to compute
        total_strategies=len(deployments),
        running_strategies=running,
        stopped_strategies=stopped,
        total_trades=total_trades,
        win_rate=win_rate,
        broker_connected=broker["connected"],
        broker_equity=broker.get("equity"),
        last_updated=datetime.utcnow().isoformat(),
    )


@router.get("/health")
async def system_health():
    """Check health of all system components."""
    broker = _check_broker()
    deployments = _load_deployments()
    running = sum(1 for d in deployments if d.get("status") == "running")

    return {
        "status": "healthy",
        "broker": broker,
        "strategies_deployed": len(deployments),
        "strategies_running": running,
        "state_files": len(list(STATE_DIR.glob("*.pkl"))) if STATE_DIR.exists() else 0,
    }


@router.get("/stats")
async def system_stats():
    """Get system statistics."""
    deployments = _load_deployments()
    return {
        "uptime_seconds": 0,
        "messages_processed": 0,
        "active_strategies": sum(1 for d in deployments if d.get("status") == "running"),
        "total_strategies": len(deployments),
        "open_positions": 0,
    }
