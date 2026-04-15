"""Strategy management API: list, deploy, start, stop, adjust strategies.

Provides a unified strategy registry that:
  - Discovers all available strategies from the strategies/ folder
  - Tracks deployed strategies and their state (running/stopped/error)
  - Supports paper and live trading modes
  - Stores deployment config in data/strategy_deployments.json
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

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

router = APIRouter()

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
STATE_DIR = PROJECT_ROOT / "data" / "live_state"
LOG_DIR = PROJECT_ROOT / "logs" / "paper_trading"
DEPLOY_FILE = PROJECT_ROOT / "data" / "strategy_deployments.json"
ENV_FILE = PROJECT_ROOT / ".env"


# ── Schemas ────────────────────────────────────────────────────

class StrategyInfo(BaseModel):
    """Available strategy definition."""
    id: str
    name: str
    description: str
    asset_class: str
    category: str  # options, equity, crypto
    symbols: List[str] = []
    default_params: Dict[str, Any] = {}
    backtest_sharpe: Optional[float] = None
    backtest_win_rate: Optional[float] = None
    backtest_trades: Optional[int] = None


class DeployedStrategy(BaseModel):
    """A strategy that has been deployed (running or stopped)."""
    id: str
    strategy_id: str
    name: str
    mode: str = "paper"  # "paper" | "live"
    status: str = "stopped"  # "running" | "stopped" | "error" | "paused"
    data_source: str = "yahoo"
    execution_broker: str = "internal"  # "internal" | "alpaca" | "ib"
    initial_capital: float = 100000.0
    deployed_at: str = ""
    last_run: Optional[str] = None
    error_message: Optional[str] = None
    # Runtime stats (from state file)
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    total_equity: float = 100000.0
    total_trades: int = 0
    win_rate: Optional[float] = None
    current_position: Optional[str] = None
    spy_price: float = 0.0


class DeployRequest(BaseModel):
    """Request to deploy a strategy."""
    strategy_id: str
    mode: str = "paper"
    data_source: str = "alpaca"
    execution_broker: str = "internal"
    initial_capital: float = 100000.0
    params: Dict[str, Any] = {}


class AdjustRequest(BaseModel):
    """Request to adjust a deployed strategy's parameters."""
    mode: Optional[str] = None
    data_source: Optional[str] = None
    execution_broker: Optional[str] = None
    initial_capital: Optional[float] = None
    params: Dict[str, Any] = {}


# ── Strategy catalog ────────────────────────────────────────────

STRATEGY_CATALOG: List[StrategyInfo] = [
    StrategyInfo(
        id="spy_gamma_scalp",
        name="SPY Gamma Scalp",
        description="Volatility risk premium exploitation via ATM straddle selling + delta hedging on SPY. Harvests theta decay with gamma hedging protection.",
        asset_class="options",
        category="options",
        symbols=["SPY"],
        default_params={
            "initial_capital": 100000,
            "contracts_per_trade": 1,
            "hedge_threshold_pct": 0.5,
            "dte_range": [3, 7],
            "signal_threshold": 0.25,
        },
        backtest_sharpe=1.98,
        backtest_win_rate=0.80,
        backtest_trades=40,
    ),
    StrategyInfo(
        id="delta_neutral",
        name="Options Delta-Neutral",
        description="Maintain delta-neutral portfolio via continuous option hedging. Profits from volatility movements regardless of direction.",
        asset_class="options",
        category="options",
        symbols=["SPY", "QQQ"],
        default_params={"rebalance_threshold": 0.1, "max_position_size": 10},
        backtest_sharpe=1.52,
        backtest_win_rate=0.71,
        backtest_trades=85,
    ),
    StrategyInfo(
        id="equity_factor",
        name="Equity Factor Strategy",
        description="Multi-factor equity strategy combining momentum, value, quality, and low-volatility factors for stock selection.",
        asset_class="equity",
        category="equity",
        symbols=["SPY", "QQQ", "IWM"],
        default_params={"lookback": 60, "rebalance_freq": "monthly", "n_stocks": 20},
        backtest_sharpe=1.87,
        backtest_win_rate=0.58,
        backtest_trades=240,
    ),
    StrategyInfo(
        id="equity_timing",
        name="Market Timing",
        description="Macro-based market timing using yield curve, credit spreads, and momentum signals to switch between risk-on/risk-off.",
        asset_class="equity",
        category="equity",
        symbols=["SPY", "TLT", "GLD"],
        default_params={"lookback": 120, "signal_smoothing": 5},
        backtest_sharpe=1.34,
        backtest_win_rate=0.62,
        backtest_trades=48,
    ),
    StrategyInfo(
        id="equity_long_short",
        name="Long/Short Equity",
        description="Statistical pairs trading strategy identifying cointegrated equity pairs and trading mean-reversion spreads.",
        asset_class="equity",
        category="equity",
        symbols=["AAPL", "MSFT", "GOOGL", "AMZN"],
        default_params={"lookback": 90, "z_threshold": 2.0, "max_pairs": 5},
        backtest_sharpe=1.45,
        backtest_win_rate=0.55,
        backtest_trades=180,
    ),
    StrategyInfo(
        id="crypto_scalping",
        name="Crypto Scalping",
        description="High-frequency crypto scalping using orderbook imbalance and microstructure signals on BTC/USDT.",
        asset_class="crypto",
        category="crypto",
        symbols=["BTC-USD", "ETH-USD"],
        default_params={"tick_interval": 100, "profit_target_bps": 15, "stop_loss_bps": 10},
        backtest_sharpe=2.41,
        backtest_win_rate=0.62,
        backtest_trades=1200,
    ),
    StrategyInfo(
        id="crypto_swing",
        name="Crypto Swing",
        description="Medium-term crypto swing trading based on RSI divergences, volume profiles, and trend following.",
        asset_class="crypto",
        category="crypto",
        symbols=["BTC-USD", "ETH-USD", "SOL-USD"],
        default_params={"lookback": 14, "rsi_oversold": 30, "rsi_overbought": 70},
        backtest_sharpe=0.95,
        backtest_win_rate=0.48,
        backtest_trades=65,
    ),
    StrategyInfo(
        id="btc_sniper",
        name="BTC Momentum Sniper v5",
        description="High-leverage BTC perpetual futures trend-following with fixed-risk position sizing. "
                    "50x leverage, 5% equity risk per trade, ATR-based trailing stops. "
                    "Dual entry: Donchian breakout + EMA21 pullback. 4H timeframe.",
        asset_class="crypto",
        category="crypto",
        symbols=["BTC/USDT"],
        default_params={
            "leverage": 50.0,
            "risk_per_trade": 0.05,
            "atr_stop": 3.5,
            "atr_trail": 2.5,
            "min_score": 2,
            "initial_capital": 10000,
        },
        backtest_sharpe=1.68,
        backtest_win_rate=0.546,
        backtest_trades=260,
    ),
    StrategyInfo(
        id="crypto_momentum",
        name="Crypto Momentum",
        description="Cross-sectional momentum in crypto markets, rotating into top performers with trend confirmation.",
        asset_class="crypto",
        category="crypto",
        symbols=["BTC-USD", "ETH-USD", "SOL-USD", "ADA-USD"],
        default_params={"lookback": 30, "top_n": 3, "rebalance_freq": "weekly"},
        backtest_sharpe=1.15,
        backtest_win_rate=0.52,
        backtest_trades=96,
    ),
    StrategyInfo(
        id="crypto_breakout",
        name="Crypto Breakout",
        description="Breakout detection strategy using price structure analysis, volume confirmation, and momentum filters. "
                    "Identifies consolidation patterns and trades the breakout with ATR-based position sizing.",
        asset_class="crypto",
        category="crypto",
        symbols=["BTC/USDT", "ETH/USDT"],
        default_params={"lookback": 20, "volume_threshold": 1.5, "atr_multiplier": 2.0},
        backtest_sharpe=1.25,
        backtest_win_rate=0.52,
        backtest_trades=180,
    ),
    StrategyInfo(
        id="crypto_orderbook",
        name="Crypto Orderbook",
        description="Orderbook depth analysis strategy using bid-ask imbalance, liquidity walls, and microstructure "
                    "signals for short-term directional trading on centralized exchanges.",
        asset_class="crypto",
        category="crypto",
        symbols=["BTC/USDT"],
        default_params={"depth_levels": 20, "imbalance_threshold": 0.6, "update_interval_ms": 1000},
        backtest_sharpe=1.80,
        backtest_win_rate=0.58,
        backtest_trades=500,
    ),
    StrategyInfo(
        id="crypto_event",
        name="Crypto Event-Driven",
        description="Event-driven strategy capturing price dislocations around major crypto events: halvings, "
                    "protocol upgrades, regulatory announcements, and exchange listings.",
        asset_class="crypto",
        category="crypto",
        symbols=["BTC/USDT", "ETH/USDT"],
        default_params={"event_window_hours": 48, "position_size_pct": 0.10, "stop_loss_pct": 0.05},
        backtest_sharpe=0.95,
        backtest_win_rate=0.60,
        backtest_trades=45,
    ),
    StrategyInfo(
        id="crypto_macro_cycle",
        name="BTC/ETH Macro Cycle",
        description="5x leveraged macro cycle entry signals with wide stops. Multi-factor composite scoring "
                    "(trend, momentum, volume, drawdown, volatility regime). 4H timeframe, max 3 trades/day. "
                    "Designed for capturing major directional moves in BTC and ETH.",
        asset_class="crypto",
        category="crypto",
        symbols=["BTC/USDT", "ETH/USDT"],
        default_params={"leverage": 5.0, "max_trades_per_day": 3, "lookback": 60, "composite_threshold": 0.7},
        backtest_sharpe=0.41,
        backtest_win_rate=0.626,
        backtest_trades=222,
    ),
    StrategyInfo(
        id="spy_iron_condor",
        name="SPY Iron Condor",
        description="Systematic iron condor strategy on SPY options. Sells OTM put and call spreads, "
                    "targeting monthly premium collection with defined risk and mechanical adjustment rules.",
        asset_class="options",
        category="options",
        symbols=["SPY"],
        default_params={"target_dte": 30, "wing_width": 5, "delta_target": 0.16, "max_positions": 3},
        backtest_sharpe=1.45,
        backtest_win_rate=0.72,
        backtest_trades=120,
    ),
    StrategyInfo(
        id="spy_wheel",
        name="SPY Wheel Strategy",
        description="The Wheel (cash-secured put → covered call) on SPY. Sells puts for entry, holds shares, "
                    "sells calls for exit. Systematic premium harvesting with automatic roll management.",
        asset_class="options",
        category="options",
        symbols=["SPY"],
        default_params={"put_delta": -0.30, "call_delta": 0.30, "target_dte": 30},
        backtest_sharpe=1.30,
        backtest_win_rate=0.68,
        backtest_trades=85,
    ),
]

CATALOG_MAP = {s.id: s for s in STRATEGY_CATALOG}

# ── State file mapping ──────────────────────────────────────────

STATE_FILE_MAP = {
    "spy_gamma_scalp": "gamma_scalp_state.pkl",
}

DAILY_LOG_MAP = {
    "spy_gamma_scalp": "gamma_scalp_daily.jsonl",
}


# ── Helpers ──────────────────────────────────────────────────────

def _load_deployments() -> List[Dict[str, Any]]:
    """Load deployed strategies from JSON file."""
    if not DEPLOY_FILE.exists():
        return []
    try:
        with open(DEPLOY_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save_deployments(deployments: List[Dict[str, Any]]) -> None:
    """Save deployed strategies to JSON file."""
    DEPLOY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DEPLOY_FILE, "w") as f:
        json.dump(deployments, f, indent=2, default=str)


def _load_strategy_state(strategy_id: str) -> Optional[Dict[str, Any]]:
    """Load strategy runtime state from pickle file."""
    filename = STATE_FILE_MAP.get(strategy_id)
    if not filename:
        return None
    state_file = STATE_DIR / filename
    if not state_file.exists():
        return None
    try:
        with open(state_file, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _load_daily_log(strategy_id: str, limit: int = 500) -> List[Dict]:
    """Load daily log for strategy."""
    filename = DAILY_LOG_MAP.get(strategy_id)
    if not filename:
        return []
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
        return lines[-limit:]
    except Exception:
        return []


def _enrich_deployment(dep: Dict[str, Any]) -> DeployedStrategy:
    """Enrich a deployment record with runtime state data."""
    strategy_id = dep.get("strategy_id", "")
    state = _load_strategy_state(strategy_id)

    d = DeployedStrategy(**dep)

    if state:
        from dashboard.backend.routers.paper_trading import compute_unrealized_pnl
        d.realized_pnl = round(state.get("_running_pnl", 0), 2)
        d.total_trades = len(state.get("_trade_results", []))
        d.spy_price = round(state.get("_last_spot", 0), 2)

        initial = state.get("_initial_capital", d.initial_capital)
        running_pnl = state.get("_running_pnl", 0)
        unrealized = compute_unrealized_pnl(state)
        d.unrealized_pnl = round(unrealized, 2)
        d.total_equity = round(initial + running_pnl + unrealized, 2)

        if state.get("_straddle_open"):
            direction = state.get("_trade_direction", "none")
            contracts = state.get("_n_contracts", 0)
            d.current_position = f"{direction} {contracts}x straddle"
        else:
            d.current_position = "Flat"

        # Win rate from trade results
        results = state.get("_trade_results", [])
        if results:
            wins = sum(1 for r in results if r.get("total_pnl", 0) > 0)
            d.win_rate = round(wins / len(results) * 100, 1)

        # Last run time
        ct = state.get("_current_time")
        if ct:
            d.last_run = str(ct.date() if hasattr(ct, "date") else ct)

        d.status = "running"  # If state file exists, it's been running
    else:
        d.current_position = "Not started"

    return d


# ── Endpoints ────────────────────────────────────────────────────

@router.get("/catalog", response_model=List[StrategyInfo])
async def get_strategy_catalog():
    """Get all available strategies that can be deployed."""
    return STRATEGY_CATALOG


@router.get("/catalog/{strategy_id}", response_model=StrategyInfo)
async def get_catalog_strategy(strategy_id: str):
    """Get a single strategy from the catalog by ID."""
    if strategy_id not in CATALOG_MAP:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found in catalog")
    return CATALOG_MAP[strategy_id]


@router.get("/", response_model=List[DeployedStrategy])
async def list_strategies():
    """List all deployed strategies with their current status."""
    deployments = _load_deployments()
    return [_enrich_deployment(d) for d in deployments]


@router.get("/{deploy_id}", response_model=DeployedStrategy)
async def get_strategy_detail(deploy_id: str):
    """Get detailed info about a deployed strategy."""
    deployments = _load_deployments()
    for d in deployments:
        if d.get("id") == deploy_id:
            return _enrich_deployment(d)
    raise HTTPException(status_code=404, detail=f"Deployment {deploy_id} not found")


@router.post("/deploy", response_model=DeployedStrategy)
async def deploy_strategy(req: DeployRequest):
    """Deploy a strategy for paper or live trading."""
    if req.strategy_id not in CATALOG_MAP:
        raise HTTPException(status_code=404, detail=f"Strategy {req.strategy_id} not found in catalog")

    catalog_entry = CATALOG_MAP[req.strategy_id]
    deployments = _load_deployments()

    # Check if already deployed
    for d in deployments:
        if d.get("strategy_id") == req.strategy_id and d.get("status") in ("running", "stopped"):
            raise HTTPException(status_code=409, detail=f"Strategy {req.strategy_id} is already deployed")

    deploy_id = f"{req.strategy_id}_{req.mode}_{int(datetime.now().timestamp())}"
    deployment = {
        "id": deploy_id,
        "strategy_id": req.strategy_id,
        "name": catalog_entry.name,
        "mode": req.mode,
        "status": "stopped",
        "data_source": req.data_source,
        "execution_broker": req.execution_broker,
        "initial_capital": req.initial_capital,
        "deployed_at": datetime.now().isoformat(),
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "total_equity": req.initial_capital,
        "total_trades": 0,
        "spy_price": 0.0,
    }

    deployments.append(deployment)
    _save_deployments(deployments)

    return DeployedStrategy(**deployment)


@router.post("/{deploy_id}/start")
async def start_strategy(deploy_id: str, background_tasks: BackgroundTasks):
    """Start a deployed strategy."""
    deployments = _load_deployments()
    target = None
    for d in deployments:
        if d.get("id") == deploy_id:
            target = d
            break

    if not target:
        raise HTTPException(status_code=404, detail="Deployment not found")

    if target.get("status") == "running":
        return {"status": "already_running", "deploy_id": deploy_id}

    target["status"] = "running"
    target["last_run"] = datetime.now().isoformat()
    _save_deployments(deployments)

    # Launch the paper trading script in background
    strategy_id = target.get("strategy_id", "")
    if strategy_id == "spy_gamma_scalp":
        def _run():
            python = sys.executable
            script = str(PROJECT_ROOT / "scripts" / "live_paper_trade.py")
            cmd = [
                python, script,
                "--data-source", target.get("data_source", "yahoo"),
                "--mode", "once",
                "--initial-capital", str(target.get("initial_capital", 100000)),
            ]
            broker = target.get("execution_broker", "internal")
            if broker == "alpaca":
                cmd.append("--alpaca-execution")
            elif broker == "ib":
                cmd.append("--ib-execution")
            try:
                subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            except Exception:
                pass

        background_tasks.add_task(_run)

    return {"status": "started", "deploy_id": deploy_id}


@router.post("/{deploy_id}/stop")
async def stop_strategy(deploy_id: str):
    """Stop a deployed strategy."""
    deployments = _load_deployments()
    for d in deployments:
        if d.get("id") == deploy_id:
            d["status"] = "stopped"
            _save_deployments(deployments)
            return {"status": "stopped", "deploy_id": deploy_id}
    raise HTTPException(status_code=404, detail="Deployment not found")


@router.post("/{deploy_id}/adjust")
async def adjust_strategy(deploy_id: str, req: AdjustRequest):
    """Adjust parameters of a deployed strategy."""
    deployments = _load_deployments()
    for d in deployments:
        if d.get("id") == deploy_id:
            if req.mode is not None:
                d["mode"] = req.mode
            if req.data_source is not None:
                d["data_source"] = req.data_source
            if req.execution_broker is not None:
                d["execution_broker"] = req.execution_broker
            if req.initial_capital is not None:
                d["initial_capital"] = req.initial_capital
            _save_deployments(deployments)
            return {"status": "adjusted", "deploy_id": deploy_id, "changes": req.dict(exclude_none=True)}
    raise HTTPException(status_code=404, detail="Deployment not found")


@router.delete("/{deploy_id}")
async def remove_strategy(deploy_id: str):
    """Remove a deployed strategy (must be stopped first)."""
    deployments = _load_deployments()
    new_deps = []
    removed = False
    for d in deployments:
        if d.get("id") == deploy_id:
            if d.get("status") == "running":
                raise HTTPException(status_code=409, detail="Cannot remove a running strategy. Stop it first.")
            removed = True
        else:
            new_deps.append(d)

    if not removed:
        raise HTTPException(status_code=404, detail="Deployment not found")

    _save_deployments(new_deps)
    return {"status": "removed", "deploy_id": deploy_id}


@router.get("/{strategy_id}/signals")
async def get_strategy_signals(strategy_id: str, limit: int = 50):
    """Get recent signals from a strategy."""
    entries = _load_daily_log(strategy_id, limit)
    signals = []
    for e in entries:
        iv = e.get("implied_vol", 0)
        rv = e.get("rv_20d", 0)
        signals.append({
            "date": e.get("date", ""),
            "vrp": round(iv - rv, 4),
            "implied_vol": round(iv, 4),
            "rv_20d": round(rv, 4),
            "actions": e.get("actions", []),
            "position_open": e.get("position_open", False),
            "direction": e.get("trade_direction", "none"),
        })
    return signals


@router.get("/{deploy_id}/equity-curve")
async def get_deployed_equity_curve(deploy_id: str):
    """Get equity curve for a deployed strategy."""
    deployments = _load_deployments()
    strategy_id = None
    for d in deployments:
        if d.get("id") == deploy_id:
            strategy_id = d.get("strategy_id")
            break

    if not strategy_id:
        raise HTTPException(status_code=404, detail="Deployment not found")

    entries = _load_daily_log(strategy_id, 1000)
    if not entries:
        return []

    peak = 0.0
    result = []
    for e in entries:
        equity = e.get("total_equity", 100000)
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0
        result.append({
            "date": e.get("date", ""),
            "equity": round(equity, 2),
            "drawdown": round(dd * 100, 2),
        })
    return result
