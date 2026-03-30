"""Backtest API endpoints: run backtests, get results, compare runs.

Stores results in-memory (for development) and on-disk as JSON.
"""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.logging import get_logger

logger = get_logger(__name__)


def _sanitize(obj):
    """Replace inf/nan with JSON-safe values recursively."""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, float):
        if math.isinf(obj) or math.isnan(obj):
            return 0.0
    return obj

router = APIRouter()

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
RESULTS_DIR = PROJECT_ROOT / "results"
DATA_DIR = PROJECT_ROOT / "data" / "sample"

# In-memory store for backtest results (maps backtest_id -> result dict)
_backtest_store: Dict[str, Dict[str, Any]] = {}
_running_tasks: Dict[str, str] = {}  # backtest_id -> status


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------

class BacktestRequest(BaseModel):
    strategy: str = "crypto_scalping"
    symbol: str = "BTC/USDT"
    data_file: Optional[str] = None  # CSV filename in data/sample/
    initial_capital: float = 100_000
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None
    tick_mode: bool = False


class BacktestSummary(BaseModel):
    backtest_id: str
    strategy: str
    symbol: str
    status: str
    created_at: str
    metrics: Optional[Dict[str, Any]] = None
    n_trades: int = 0
    final_value: float = 0


# ---------------------------------------------------------------------------
# Background task: run backtest
# ---------------------------------------------------------------------------

async def _run_backtest_task(backtest_id: str, request: BacktestRequest):
    """Run a backtest in the background."""
    import pandas as pd

    from backtesting.engine.event_driven import EventDrivenBacktest
    from backtesting.engine.tick_replay import TickBacktestEngine
    from execution.models.fees import CryptoMakerTakerFee
    from execution.models.slippage import FixedSlippage
    from strategies.base import StrategyConfig
    from strategies.registry import create_strategy, discover_strategies

    try:
        _running_tasks[backtest_id] = "running"
        logger.info("backtest_task_started", backtest_id=backtest_id, strategy=request.strategy)

        discover_strategies()

        # Load data
        data_file = request.data_file
        if not data_file:
            # Auto-detect: look for matching CSV in data/sample/
            safe_symbol = request.symbol.replace("/", "_")
            candidates = [
                DATA_DIR / f"{safe_symbol}_4h.csv",
                DATA_DIR / f"{safe_symbol}_1d.csv",
                DATA_DIR / f"{safe_symbol}_1h.csv",
                DATA_DIR / f"{safe_symbol}_1m.csv",
                DATA_DIR / f"{safe_symbol}_trades.csv",
                DATA_DIR / f"{safe_symbol}.csv",
            ]
            for c in candidates:
                if c.exists():
                    data_file = str(c)
                    break

        if not data_file or not Path(data_file).exists():
            # Check if it's a relative path from data/sample/
            if data_file:
                full_path = DATA_DIR / data_file
                if full_path.exists():
                    data_file = str(full_path)
                else:
                    raise FileNotFoundError(f"Data file not found: {data_file}")
            else:
                raise FileNotFoundError(
                    f"No data file found for {request.symbol}. "
                    f"Run: python scripts/fetch_market_data.py --symbol {request.symbol}"
                )

        df = pd.read_csv(data_file, parse_dates=True)
        df.columns = [c.lower() for c in df.columns]

        # Ensure timestamp column
        if "timestamp" not in df.columns:
            for col in ("time", "datetime", "date"):
                if col in df.columns:
                    df.rename(columns={col: "timestamp"}, inplace=True)
                    break
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

        # Apply date filters (use UTC-aware timestamps for comparison)
        if request.start_date and "timestamp" in df.columns:
            df = df[df["timestamp"] >= pd.Timestamp(request.start_date, tz="UTC")]
        if request.end_date and "timestamp" in df.columns:
            df = df[df["timestamp"] <= pd.Timestamp(request.end_date, tz="UTC")]

        data = {request.symbol: df}

        # Create strategy
        params = request.parameters or {}
        config = StrategyConfig(
            strategy_id=f"bt_{backtest_id[:8]}",
            name=request.strategy,
            symbols=[request.symbol],
            parameters=params,
            mode="backtest",
        )
        strategy = create_strategy(request.strategy, config)

        # Choose engine
        if request.tick_mode and ("price" in df.columns or "bid" in df.columns):
            engine = TickBacktestEngine(
                strategy=strategy,
                initial_cash=request.initial_capital,
                slippage=FixedSlippage(bps=2),
                fees=CryptoMakerTakerFee(maker=0.0002, taker=0.0005),
            )
        else:
            engine = EventDrivenBacktest(
                strategy=strategy,
                initial_cash=request.initial_capital,
                slippage=FixedSlippage(bps=2),
                fees=CryptoMakerTakerFee(maker=0.0002, taker=0.0005),
                risk_check=False,
            )

        # Run
        results = await engine.run(data)

        # Store result (sanitize inf/nan for JSON safety)
        result_record = _sanitize({
            "backtest_id": backtest_id,
            "strategy": request.strategy,
            "symbol": request.symbol,
            "status": "completed",
            "created_at": datetime.utcnow().isoformat(),
            "initial_capital": request.initial_capital,
            "parameters": params,
            "metrics": results.get("metrics", {}),
            "n_trades": results.get("n_trades", 0),
            "final_value": results.get("final_value", request.initial_capital),
            "equity_curve": results.get("equity_curve", []),
            "trades": results.get("trades", []),
        })

        _backtest_store[backtest_id] = result_record
        _running_tasks[backtest_id] = "completed"

        # Persist to disk
        RESULTS_DIR.mkdir(exist_ok=True)
        with open(RESULTS_DIR / f"{backtest_id}.json", "w") as f:
            json.dump(result_record, f, indent=2, default=str)

        logger.info(
            "backtest_task_completed",
            backtest_id=backtest_id,
            n_trades=results.get("n_trades", 0),
            sharpe=results.get("metrics", {}).get("sharpe_ratio", 0),
        )

    except Exception as e:
        logger.error("backtest_task_failed", backtest_id=backtest_id, error=str(e))
        _backtest_store[backtest_id] = {
            "backtest_id": backtest_id,
            "strategy": request.strategy,
            "symbol": request.symbol,
            "status": "failed",
            "created_at": datetime.utcnow().isoformat(),
            "error": str(e),
            "metrics": {},
            "n_trades": 0,
            "final_value": 0,
            "equity_curve": [],
            "trades": [],
        }
        _running_tasks[backtest_id] = "failed"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/run")
async def run_backtest(request: BacktestRequest, background_tasks: BackgroundTasks):
    """Launch a backtest in the background.

    Returns immediately with a backtest_id for polling.
    """
    backtest_id = str(uuid.uuid4())[:12]

    _running_tasks[backtest_id] = "queued"
    _backtest_store[backtest_id] = {
        "backtest_id": backtest_id,
        "strategy": request.strategy,
        "symbol": request.symbol,
        "status": "running",
        "created_at": datetime.utcnow().isoformat(),
        "metrics": {},
        "n_trades": 0,
        "final_value": 0,
        "equity_curve": [],
        "trades": [],
    }

    background_tasks.add_task(_run_backtest_task, backtest_id, request)

    return {
        "backtest_id": backtest_id,
        "status": "queued",
        "message": f"Backtest queued for {request.strategy} on {request.symbol}",
    }


@router.get("/status/{backtest_id}")
async def get_backtest_status(backtest_id: str):
    """Check backtest execution status."""
    status = _running_tasks.get(backtest_id, "not_found")
    return {"backtest_id": backtest_id, "status": status}


@router.get("/results")
async def list_backtest_results():
    """List all backtest results (in-memory + on-disk)."""
    # Load from disk if not in memory
    if RESULTS_DIR.exists():
        for f in RESULTS_DIR.glob("*.json"):
            bid = f.stem
            if bid not in _backtest_store:
                try:
                    with open(f) as fh:
                        _backtest_store[bid] = json.load(fh)
                except Exception:
                    pass

    summaries = []
    for bid, result in sorted(_backtest_store.items(), key=lambda x: x[1].get("created_at", ""), reverse=True):
        summaries.append(_sanitize({
            "backtest_id": bid,
            "strategy": result.get("strategy", ""),
            "symbol": result.get("symbol", ""),
            "status": result.get("status", "unknown"),
            "created_at": result.get("created_at", ""),
            "metrics": result.get("metrics", {}),
            "n_trades": result.get("n_trades", 0),
            "final_value": result.get("final_value", 0),
        }))

    return summaries


@router.get("/results/{backtest_id}")
async def get_backtest_result(backtest_id: str):
    """Get detailed backtest result including equity curve and trades."""
    # Check memory first
    if backtest_id in _backtest_store:
        return _sanitize(_backtest_store[backtest_id])

    # Check disk
    result_file = RESULTS_DIR / f"{backtest_id}.json"
    if result_file.exists():
        with open(result_file) as f:
            result = json.load(f)
        _backtest_store[backtest_id] = result
        return _sanitize(result)

    raise HTTPException(status_code=404, detail=f"Backtest {backtest_id} not found")


# ---------------------------------------------------------------------------
# Gamma Scalp dedicated endpoint (analytical P&L, not event-driven engine)
# ---------------------------------------------------------------------------

class GammaScalpRequest(BaseModel):
    initial_capital: float = 100_000
    dte: int = 5
    base_contracts: int = 30
    sell_threshold: float = 0.25
    buy_threshold: float = 0.80
    max_loss_pct: float = 0.20
    max_notional_pct: float = 0.45
    take_profit_pct: float = 0.70
    vix_floor: float = 12.0
    vix_ceiling: float = 40.0
    hedge_threshold: float = 0.06


async def _run_gamma_scalp_task(backtest_id: str, request: GammaScalpRequest):
    """Run gamma scalp backtest using analytical P&L tracking."""
    import sys
    sys.path.insert(0, str(PROJECT_ROOT))

    try:
        _running_tasks[backtest_id] = "running"

        import pandas as pd
        from core.enums import AssetClass, TimeFrame
        from core.models import Bar
        from strategies.base import StrategyConfig
        from strategies.registry import discover_strategies, create_strategy
        from strategies.options.gamma_scalp_strategy import bs_straddle
        from backtesting.metrics.performance import compute_metrics
        from backtesting.metrics.risk_metrics import compute_risk_metrics
        import numpy as np

        discover_strategies()

        data_path = PROJECT_ROOT / "data" / "sample" / "SPY_gamma_scalp_daily.csv"
        if not data_path.exists():
            raise FileNotFoundError("SPY data not found. Run: python scripts/fetch_spy_options_data.py")

        df = pd.read_csv(data_path, parse_dates=["timestamp"])

        params = {
            "dte": request.dte,
            "base_contracts": request.base_contracts,
            "sell_signal_threshold": request.sell_threshold,
            "buy_signal_threshold": request.buy_threshold,
            "max_loss_pct": request.max_loss_pct,
            "max_notional_pct": request.max_notional_pct,
            "take_profit_pct": request.take_profit_pct,
            "vix_floor": request.vix_floor,
            "vix_ceiling": request.vix_ceiling,
            "hedge_delta_threshold": request.hedge_threshold,
            "initial_capital": request.initial_capital,
        }

        config = StrategyConfig(
            strategy_id=f"gamma_{backtest_id[:8]}",
            name="SPY Gamma Scalp v2",
            asset_class=AssetClass.EQUITY,
            symbols=["SPY"],
            parameters=params,
            mode="backtest",
        )
        strategy = create_strategy("spy_gamma_scalp", config)
        strategy.initialize()

        equity_curve = []
        trades = []
        valid = df.dropna(subset=["close"]).copy()
        prev_n = 0

        for idx, row in valid.iterrows():
            ts = row["timestamp"].to_pydatetime()
            spot = float(row["close"])
            metadata = {}
            for col in ["implied_vol", "risk_free_rate", "vix_close"]:
                if col in row and not pd.isna(row[col]):
                    metadata[col] = float(row[col])

            bar = Bar(
                symbol="SPY", timestamp=ts,
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=spot,
                volume=float(row.get("volume", 0)),
                timeframe=TimeFrame.D1, asset_class=AssetClass.EQUITY,
                metadata=metadata,
            )
            strategy.on_bar("SPY", bar)
            strategy.calculate_target_positions()

            n = len(strategy._trade_results)
            if n > prev_n:
                for t in strategy._trade_results[prev_n:]:
                    trades.append({
                        "timestamp": t["timestamp"],
                        "symbol": "SPY",
                        "side": "SELL_STRADDLE" if t["direction"] == "sell_vol" else "BUY_STRADDLE",
                        "price": t["entry_spot"],
                        "quantity": t["contracts"],
                        "commission": t["txn_costs"],
                        "pnl": t["total_pnl"],
                        "reason": t["reason"],
                    })
                prev_n = n

            realized = strategy._running_pnl
            unrealized = 0.0
            if strategy._straddle_open and strategy._n_contracts > 0:
                T = strategy._time_to_expiry()
                cp, pp = bs_straddle(spot, strategy._strike, T, strategy._risk_free, strategy._implied_vol)
                ntl = strategy._n_contracts * strategy.multiplier
                sign = -1.0 if strategy._trade_direction == "sell_vol" else 1.0
                unrealized = ((cp + pp) - (strategy._entry_call_price + strategy._entry_put_price)) * ntl * sign
                unrealized += strategy._cum_hedge_pnl - strategy._cum_txn_costs

            total_value = request.initial_capital + realized + unrealized
            equity_curve.append({
                "timestamp": ts.isoformat(),
                "total_value": round(total_value, 2),
                "cash": round(request.initial_capital + realized, 2),
                "unrealized_pnl": round(unrealized, 2),
                "n_positions": 1 if strategy._straddle_open else 0,
            })

        # Compute metrics
        eq_df = pd.DataFrame(equity_curve)
        eq_df["total_value"] = eq_df["total_value"].astype(float)
        metrics = compute_metrics(eq_df, risk_free_rate=0.045, periods_per_year=252)

        eq_df["returns"] = eq_df["total_value"].pct_change().fillna(0)
        risk = compute_risk_metrics(eq_df["returns"].dropna(), confidence=0.95, portfolio_value=request.initial_capital)

        # Strategy analytics
        tr = strategy._trade_results
        sell_trades = [t for t in tr if t["direction"] == "sell_vol"]
        buy_trades = [t for t in tr if t["direction"] == "buy_vol"]

        strategy_analytics = {
            "total_trades": len(tr),
            "sell_vol_trades": len(sell_trades),
            "buy_vol_trades": len(buy_trades),
            "sell_vol_pnl": round(sum(t["total_pnl"] for t in sell_trades), 2),
            "buy_vol_pnl": round(sum(t["total_pnl"] for t in buy_trades), 2),
            "sell_win_rate": round(sum(1 for t in sell_trades if t["total_pnl"] > 0) / max(len(sell_trades), 1) * 100, 1),
            "buy_win_rate": round(sum(1 for t in buy_trades if t["total_pnl"] > 0) / max(len(buy_trades), 1) * 100, 1),
            "avg_gamma_pnl": round(float(np.mean([t["gamma_pnl"] for t in tr])), 2) if tr else 0,
            "avg_theta_pnl": round(float(np.mean([t["theta_pnl"] for t in tr])), 2) if tr else 0,
            "avg_vega_pnl": round(float(np.mean([t["vega_pnl"] for t in tr])), 2) if tr else 0,
            "total_txn_costs": round(sum(t["txn_costs"] for t in tr), 2),
        }

        result = _sanitize({
            "backtest_id": backtest_id,
            "strategy": "spy_gamma_scalp",
            "strategy_name": "SPY Gamma Scalp v2",
            "symbol": "SPY",
            "status": "completed",
            "created_at": datetime.utcnow().isoformat(),
            "initial_capital": request.initial_capital,
            "parameters": params,
            "metrics": metrics,
            "risk_metrics": risk,
            "strategy_analytics": strategy_analytics,
            "trade_results": tr,
            "n_trades": len(trades),
            "final_value": equity_curve[-1]["total_value"] if equity_curve else request.initial_capital,
            "equity_curve": equity_curve,
            "trades": trades,
            "n_bars": len(valid),
        })

        _backtest_store[backtest_id] = result
        _running_tasks[backtest_id] = "completed"

        RESULTS_DIR.mkdir(exist_ok=True)
        with open(RESULTS_DIR / f"{backtest_id}.json", "w") as f:
            json.dump(result, f, indent=2, default=str)

    except Exception as e:
        import traceback
        logger.error("gamma_scalp_task_failed", error=str(e), tb=traceback.format_exc())
        _backtest_store[backtest_id] = {
            "backtest_id": backtest_id,
            "strategy": "spy_gamma_scalp",
            "symbol": "SPY",
            "status": "failed",
            "created_at": datetime.utcnow().isoformat(),
            "error": str(e),
            "metrics": {},
            "n_trades": 0,
            "final_value": 0,
            "equity_curve": [],
            "trades": [],
        }
        _running_tasks[backtest_id] = "failed"


@router.post("/run-gamma-scalp")
async def run_gamma_scalp(request: GammaScalpRequest, background_tasks: BackgroundTasks):
    """Launch SPY Gamma Scalp backtest (analytical pricing)."""
    backtest_id = str(uuid.uuid4())[:12]
    _running_tasks[backtest_id] = "queued"
    _backtest_store[backtest_id] = {
        "backtest_id": backtest_id,
        "strategy": "spy_gamma_scalp",
        "symbol": "SPY",
        "status": "running",
        "created_at": datetime.utcnow().isoformat(),
        "metrics": {},
        "n_trades": 0,
        "final_value": 0,
        "equity_curve": [],
        "trades": [],
    }
    background_tasks.add_task(_run_gamma_scalp_task, backtest_id, request)
    return {
        "backtest_id": backtest_id,
        "status": "queued",
        "message": "SPY Gamma Scalp backtest queued",
    }


@router.get("/data-files")
async def list_data_files():
    """List available data files for backtesting."""
    files = []
    if DATA_DIR.exists():
        for f in sorted(DATA_DIR.glob("*.csv")):
            size_mb = f.stat().st_size / (1024 * 1024)
            # Read first line to get column info
            try:
                import pandas as pd
                df_head = pd.read_csv(f, nrows=2)
                n_rows = sum(1 for _ in open(f)) - 1
                cols = list(df_head.columns)
            except Exception:
                n_rows = 0
                cols = []

            files.append({
                "filename": f.name,
                "path": str(f),
                "size_mb": round(size_mb, 2),
                "rows": n_rows,
                "columns": cols,
            })
    return files


@router.get("/strategies")
async def list_available_strategies():
    """List strategies available for backtesting."""
    from strategies.registry import discover_strategies, list_strategies
    discover_strategies()
    return list_strategies()
