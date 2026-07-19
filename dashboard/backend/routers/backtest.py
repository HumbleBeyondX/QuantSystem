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


def _normalize_result(bid: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a backtest result loaded from disk to the standard API format.

    Different backtest engines save results with different metric key names
    and structures. This function maps them all to the standard format that
    the frontend expects:
      - metrics.sharpe_ratio, metrics.max_drawdown_pct, metrics.sortino_ratio, etc.
      - top-level: backtest_id, strategy, status, n_trades, final_value
    """
    # --- Ensure top-level fields exist ---
    result = dict(raw)
    result.setdefault("backtest_id", bid)
    result.setdefault("status", "completed" if result.get("metrics") else "unknown")
    result.setdefault("symbol", "")
    result.setdefault("created_at", "")

    metrics = dict(result.get("metrics", {}))

    # --- Metric key aliases: old_key -> standard_key ---
    _METRIC_ALIASES = {
        "sharpe":           "sharpe_ratio",
        "sortino":          "sortino_ratio",
        "calmar":           "calmar_ratio",
        "max_dd_pct":       "max_drawdown_pct",
        "volatility":       "annual_volatility",
    }
    for old_key, new_key in _METRIC_ALIASES.items():
        if old_key in metrics and new_key not in metrics:
            metrics[new_key] = metrics[old_key]

    # --- win_loss_ratio from avg_win / avg_loss ---
    if "win_loss_ratio" not in metrics and "avg_win" in metrics and "avg_loss" in metrics:
        avg_loss = abs(metrics["avg_loss"]) if metrics["avg_loss"] != 0 else 0.01
        metrics["win_loss_ratio"] = round(abs(metrics["avg_win"]) / avg_loss, 2)

    # --- profit_factor ---
    if "profit_factor" not in metrics:
        metrics["profit_factor"] = 0

    result["metrics"] = metrics

    # --- Top-level n_trades: prefer top-level, fallback to metrics.trades or metrics.total_trades ---
    if not result.get("n_trades"):
        result["n_trades"] = metrics.get("total_trades", metrics.get("trades", 0))

    # --- Top-level final_value: prefer top-level, fallback to metrics.end_eq or metrics.end_value ---
    if not result.get("final_value"):
        result["final_value"] = metrics.get("end_eq", metrics.get("end_value", 0))

    # --- Infer strategy from filename if missing ---
    if not result.get("strategy"):
        result["strategy"] = bid.rsplit("_", 1)[0] if "_" in bid else bid

    # --- Normalize equity_curve format: {ts, equity} -> {timestamp, total_value} ---
    eq = result.get("equity_curve")
    if eq and isinstance(eq, list) and len(eq) > 0:
        sample = eq[0]
        if "equity" in sample and "total_value" not in sample:
            result["equity_curve"] = [
                {"timestamp": pt.get("ts", pt.get("timestamp", "")), "total_value": pt["equity"], "cash": pt.get("cash", pt["equity"])}
                for pt in eq
            ]

    return result

from core.paths import get_data_dir, get_resource_root  # noqa: E402

router = APIRouter()

# Resource root = bundled scripts/config/data-samples (read-only).
# Results go to user data dir so they survive app upgrades.
PROJECT_ROOT = get_resource_root()
RESULTS_DIR = get_data_dir() / "results"
DATA_DIR = PROJECT_ROOT / "data" / "sample"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

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

async def _run_sniper_backtest(df, request: "BacktestRequest") -> Dict[str, Any]:
    """Run BTC Sniper v5 backtest using standalone fixed-risk engine.

    Uses BTCSniperBacktest which tracks:
      equity = cash + margin + unrealized_pnl (leveraged perpetual futures model)
      Position sizing: qty = risk_amount / stop_distance (fixed-risk)
    """
    import yaml

    from scripts.backtest_btc_sniper import BTCSniperBacktest

    # Load config from YAML
    config_path = PROJECT_ROOT / "config" / "strategies" / "btc_sniper.yaml"
    params = {}
    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
            params = raw.get("parameters", raw)

    # Override with request parameters
    if request.parameters:
        params.update(request.parameters)

    bt = BTCSniperBacktest(
        initial_capital=request.initial_capital,
        leverage=params.get("leverage", 50.0),
        risk_per_trade=params.get("risk_per_trade", 0.05),
        max_margin_pct=params.get("max_margin_pct", 0.30),
        min_score=params.get("min_score", 2),
        vol_min=params.get("vol_min", 1.2),
        use_donchian10=params.get("use_donchian10", True),
        atr_stop=params.get("atr_stop", 3.5),
        atr_trail=params.get("atr_trail", 2.5),
        atr_trail_activate=params.get("atr_trail_activate", 2.0),
        cooldown=params.get("cooldown", 3),
        max_hold=params.get("max_hold", 120),
        fee_rate=params.get("fee_rate", 0.0005),
        pause_after_losses=params.get("pause_after_losses", 5),
        pause_bars=params.get("pause_bars", 18),
    )

    # Run in thread pool to avoid blocking
    loop = asyncio.get_event_loop()
    raw_results = await loop.run_in_executor(None, bt.run, df)

    # Map to standard format expected by the API
    raw_metrics = raw_results.get("metrics", {})
    eq_curve = raw_results.get("equity_curve", [])
    trades = raw_results.get("trades", [])

    # Remap metric keys to standard names the frontend expects
    metrics = {
        "total_return_pct": raw_metrics.get("total_return_pct", 0),
        "annual_return_pct": raw_metrics.get("annual_return_pct", 0),
        "sharpe_ratio": raw_metrics.get("sharpe", 0),
        "sortino_ratio": raw_metrics.get("sortino", 0),
        "calmar_ratio": raw_metrics.get("calmar", 0),
        "max_drawdown_pct": raw_metrics.get("max_dd_pct", 0),
        "annual_volatility": raw_metrics.get("volatility", 0),
        "win_rate": raw_metrics.get("win_rate", 0),
        "profit_factor": raw_metrics.get("profit_factor", 0),
        "win_loss_ratio": round(
            abs(raw_metrics.get("avg_win", 0)) / max(abs(raw_metrics.get("avg_loss", 1)), 0.01), 2
        ),
        "total_trades": raw_metrics.get("trades", 0),
        "avg_win": raw_metrics.get("avg_win", 0),
        "avg_loss": raw_metrics.get("avg_loss", 0),
        "avg_bars_held": raw_metrics.get("avg_bars", 0),
        "monthly_hit_rate": raw_metrics.get("monthly_hit", 0),
        "best_month_pct": raw_metrics.get("best_month_pct", 0),
        "worst_month_pct": raw_metrics.get("worst_month_pct", 0),
        "yearly_returns": raw_metrics.get("yearly", {}),
        "exit_reasons": raw_metrics.get("exits", {}),
        "entry_types": raw_metrics.get("entry_types", {}),
        "leverage": params.get("leverage", 50.0),
    }

    # Convert equity curve to standard format {timestamp, total_value}
    std_eq = [{"timestamp": e["ts"], "total_value": round(e["equity"], 2)} for e in eq_curve]

    # Convert trades to standard format
    std_trades = []
    for t in trades:
        if t["action"].startswith("CLOSE"):
            std_trades.append({
                "timestamp": t.get("ts", ""),
                "symbol": request.symbol,
                "side": "SELL" if "LONG" in t["action"] else "BUY",
                "price": t.get("price", 0),
                "quantity": t.get("qty", 0),
                "commission": t.get("fee", 0),
                "pnl": t.get("pnl", 0),
                "reason": t.get("reason", ""),
            })
        else:
            std_trades.append({
                "timestamp": t.get("ts", ""),
                "symbol": request.symbol,
                "side": "BUY" if t["action"] == "LONG" else "SELL",
                "price": t.get("price", 0),
                "quantity": t.get("qty", 0),
                "commission": t.get("fee", 0),
            })

    n_closed = sum(1 for t in trades if t["action"].startswith("CLOSE"))

    return {
        "strategy_id": "btc_sniper",
        "initial_cash": request.initial_capital,
        "equity_curve": std_eq,
        "trades": std_trades,
        "metrics": metrics,
        "n_trades": n_closed,
        "final_value": raw_metrics.get("end_eq", request.initial_capital),
    }


async def _run_leveraged_backtest(df, request: "BacktestRequest") -> Dict[str, Any]:
    """Run leveraged futures backtest using standalone engine.

    The EventDrivenBacktest PositionManager doesn't support margin/leverage.
    This uses the MacroCycleBacktest which correctly tracks:
      equity = cash + unrealized_pnl  (leveraged futures model)
    """
    import yaml

    from scripts.backtest_macro_cycle import MacroCycleBacktest

    # Load config from YAML
    config_path = PROJECT_ROOT / "config" / "strategies" / "crypto_macro_cycle.yaml"
    params = {}
    if config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
            params = raw.get("parameters", raw)

    # Override with request parameters
    if request.parameters:
        params.update(request.parameters)

    bt = MacroCycleBacktest(
        initial_capital=request.initial_capital,
        leverage=params.get("leverage", 10.0),
        entry_threshold=params.get("entry_threshold", 0.25),
        exit_threshold=params.get("exit_threshold", -0.10),
        strong_entry_threshold=params.get("strong_entry_threshold", 0.45),
        short_entry_threshold=params.get("short_entry_threshold", -0.25),
        short_exit_threshold=params.get("short_exit_threshold", 0.10),
        atr_stop_mult=params.get("atr_stop_mult", 1.5),
        atr_tp_mult=params.get("atr_tp_mult", 3.0),
        trailing_stop_atr=params.get("trailing_stop_atr", 1.5),
        base_position_pct=params.get("base_position_pct", 0.10),
        max_position_pct=params.get("max_position_pct", 0.25),
        max_drawdown_pct=params.get("max_drawdown_pct", 0.15),
        cooldown_bars=params.get("cooldown_bars", 3),
        max_trades_per_day=params.get("max_trades_per_day", 3),
        fee_rate=params.get("fee_rate", 0.0005),
        signal_weights=params.get("signal_weights"),
    )

    # Run in thread pool to avoid blocking
    import asyncio
    loop = asyncio.get_event_loop()
    raw_results = await loop.run_in_executor(None, bt.run, df)

    # Map to standard format expected by the API
    metrics = raw_results.get("metrics", {})
    eq_curve = raw_results.get("equity_curve", [])
    trades = raw_results.get("trades", [])

    # Convert trades to standard format
    std_trades = []
    for t in trades:
        if t["action"].startswith("CLOSE"):
            std_trades.append({
                "timestamp": t.get("timestamp", ""),
                "symbol": request.symbol,
                "side": "SELL" if "LONG" in t["action"] else "BUY",
                "price": t.get("price", 0),
                "quantity": t.get("quantity", 0),
                "commission": t.get("fee", 0),
                "pnl": t.get("pnl", 0),
                "reason": t.get("reason", ""),
            })
        else:
            std_trades.append({
                "timestamp": t.get("timestamp", ""),
                "symbol": request.symbol,
                "side": "BUY" if t["action"] == "LONG" else "SELL",
                "price": t.get("price", 0),
                "quantity": t.get("quantity", 0),
                "commission": t.get("fee", 0),
            })

    return {
        "strategy_id": "crypto_macro_cycle",
        "initial_cash": request.initial_capital,
        "equity_curve": eq_curve,
        "trades": std_trades,
        "metrics": metrics,
        "n_trades": metrics.get("total_trades", len(std_trades) // 2),
        "final_value": metrics.get("end_value", request.initial_capital),
    }


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

        # ── Use standalone leveraged futures engine for crypto_macro_cycle ──
        # The EventDrivenBacktest's PositionManager doesn't support leverage/margin.
        # For leveraged strategies, use the standalone backtest that correctly tracks
        # equity = cash + unrealized_pnl (not cash + position * price).
        if request.strategy == "btc_sniper":
            results = await _run_sniper_backtest(df, request)
        elif request.strategy == "crypto_macro_cycle":
            results = await _run_leveraged_backtest(df, request)
        else:
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
            "parameters": request.parameters or {},
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
                        raw = json.load(fh)
                    _backtest_store[bid] = _normalize_result(bid, raw)
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
            raw = json.load(f)
        result = _normalize_result(backtest_id, raw)
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
            "parameters": request.parameters or {},
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


# ---------------------------------------------------------------------------
# Validated backtest records — the permanent research registry.
#
# Unlike ad-hoc runs above (ephemeral, in-memory), records are the OFFICIAL
# results that drove deploy/reject decisions. Each maps to a JSON produced
# by its research script under results/, and can be REPRODUCED from the UI
# by re-running that script (real data, same code path).
# ---------------------------------------------------------------------------

REPO_RESULTS = PROJECT_ROOT / "results"

# rid → (json file, reproduce script, optional equity-curves CSV)
RECORD_MAP: Dict[str, Dict[str, Optional[str]]] = {
    "combo_wheel_sniper": {
        "json": "combo_wheel_sniper_research.json",
        "script": "scripts/backtest_combo_wheel_sniper.py",
        "curves": "combo_wheel_sniper_curves.csv",
    },
    "wheel_delta_strike": {
        "json": "wheel_delta_strike_research.json",
        "script": "scripts/backtest_wheel_delta_strike.py",
        "curves": "wheel_delta_strike_curves.csv",
    },
    "combo_wheel_sniper_v2": {
        "json": "combo_wheel_sniper_v2_research.json",
        "script": "scripts/backtest_combo_wheel_sniper_v2.py",
        "curves": "combo_wheel_sniper_v2_curves.csv",
    },
    "tsmom_research": {
        "json": "tsmom_research.json",
        "script": "scripts/backtest_tsmom_diversified.py",
        "curves": None,
    },
    "pairs_trading_research": {
        "json": "pairs_trading_research.json",
        "script": "scripts/backtest_pairs_trading.py",
        "curves": None,
    },
    "xs_reversal_research": {
        "json": "xs_reversal_research.json",
        "script": "scripts/backtest_xs_reversal.py",
        "curves": None,
    },
    "sp500_sector_neutral_qmom": {
        "json": "sp500_sector_neutral_qmom.json",
        "script": "scripts/backtest_sp500_sector_neutral_qmom.py",
        "curves": None,
    },
}

_rerun_tasks: Dict[str, Dict[str, Any]] = {}


def _record_summary(rid: str) -> Optional[Dict[str, Any]]:
    spec = RECORD_MAP.get(rid)
    if not spec:
        return None
    path = REPO_RESULTS / spec["json"]
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    # Normalize headline stats across heterogeneous research JSONs
    headline: Dict[str, Any] = {}
    if rid == "wheel_delta_strike":
        headline = dict(data.get("headline", {}))
        status = data.get("status", "validated")
        verdict = ("GATE PASS (full post-incident protocol: IS-only selection, "
                   "blind OOS 2.07, fee/WF/LOO suite, beta check)")
    elif rid in ("combo_wheel_sniper", "combo_wheel_sniper_v2"):
        rec = data.get("combos", {}).get(data.get("recommended", "70/30"), {})
        headline = {
            "sharpe": rec.get("full", {}).get("sharpe"),
            "oos_sharpe": rec.get("oos", {}).get("sharpe"),
            "max_dd": rec.get("full", {}).get("max_dd"),
            "cagr": rec.get("full", {}).get("cagr"),
            "correlation": data.get("correlation", {}).get("full"),
        }
        if rid == "combo_wheel_sniper":
            status, verdict = "invalidated", (
                "INVALIDATED 2026-07-12 — sniper-leg engine capped losses at "
                "-margin*0.95 without liquidating; see combo_wheel_sniper_v2 "
                "for the honest re-run (found by NautilusTrader parity pilot)")
        else:
            status, verdict = "rejected", (
                "Honest re-run: combo 70/30 OOS 0.32 < 1.5 — auto-rejected. "
                "Wheel-only best mix (OOS 1.42, still below gate)")
    else:
        perf = data.get("performance", {})
        headline = {
            "sharpe": (perf.get("full") or {}).get("sharpe"),
            "oos_sharpe": (perf.get("oos") or {}).get("sharpe"),
            "max_dd": (perf.get("full") or {}).get("max_drawdown"),
        }
        if not any(v is not None for v in headline.values()):
            # Research JSONs are heterogeneous — fall back to the registry
            # entry linking this record (numbers recorded at decision time)
            from dashboard.backend.routers.strategies import STRATEGY_CATALOG
            for s in STRATEGY_CATALOG:
                if s.links.get("record") == rid and s.id != "eth_sniper":
                    headline = {"sharpe": s.metrics.get("backtest_sharpe"),
                                "oos_sharpe": s.metrics.get("oos_sharpe"),
                                "max_dd": s.metrics.get("max_dd")}
                    break
        status, verdict = "rejected", "OOS Sharpe < 1.5 — auto-rejected"
    headline = {k: (round(v, 3) if isinstance(v, float) else v)
                for k, v in headline.items() if v is not None}
    return {
        "id": rid,
        "name": data.get("name") or data.get("strategy", rid),
        "status": status,
        "verdict": verdict,
        "created": data.get("created") or data.get("generated_at"),
        "period": data.get("period"),
        "headline": {k: v for k, v in headline.items() if v is not None},
        "data_note": data.get("data"),
        "script": spec["script"],
        "has_curve": bool(spec["curves"]),
        "rerun": _rerun_tasks.get(rid, {}).get("status"),
    }


@router.get("/records")
async def list_backtest_records():
    """Official research records (validated + rejected), gate rule attached."""
    records = [r for r in (_record_summary(rid) for rid in RECORD_MAP) if r]
    records += _promoted_summaries()
    order = {"validated": 0, "candidate": 1, "rejected": 2}
    records.sort(key=lambda r: (order.get(r["status"], 3), r["id"]))
    return {"gate_rule": "OOS Sharpe >= 1.5 or auto-reject", "records": records}


@router.get("/records/{rid}")
async def get_backtest_record(rid: str):
    spec = RECORD_MAP.get(rid)
    if not spec:
        raise HTTPException(404, f"unknown record: {rid}")
    path = REPO_RESULTS / spec["json"]
    if not path.exists():
        raise HTTPException(404, f"record file missing: {spec['json']}")
    return {"summary": _record_summary(rid),
            "detail": json.loads(path.read_text(encoding="utf-8"))}


@router.get("/records/{rid}/curve")
async def get_record_curve(rid: str, max_points: int = 400):
    """Equity curves for a record (per leg + recommended combo)."""
    spec = RECORD_MAP.get(rid)
    if not spec or not spec["curves"]:
        raise HTTPException(404, "no curve data for this record")
    import pandas as pd
    df = pd.read_csv(REPO_RESULTS / spec["curves"], index_col=0, parse_dates=True)
    series: Dict[str, Any] = {}
    if rid == "combo_wheel_sniper":
        rw = df["wheel"].pct_change().fillna(0)
        rs = df["sniper"].pct_change().fillna(0)
        df = df.copy()
        df["combo_70_30"] = 100_000 * (1 + 0.7 * rw + 0.3 * rs).cumprod()
        # Rebase legs to 100k so curves are visually comparable
        for col in ("wheel", "sniper"):
            df[col] = 100_000 * df[col] / df[col].iloc[0]
    step = max(1, len(df) // max_points)
    sampled = df.iloc[::step]
    for col in sampled.columns:
        series[col] = [{"date": str(d.date()), "equity": round(float(v), 2)}
                       for d, v in sampled[col].items()]
    return {"id": rid, "series": series}


@router.post("/records/{rid}/rerun")
async def rerun_backtest_record(rid: str, background_tasks: BackgroundTasks):
    """Reproduce a record by re-running its research script (same code, real data)."""
    spec = RECORD_MAP.get(rid)
    if not spec:
        raise HTTPException(404, f"unknown record: {rid}")
    if _rerun_tasks.get(rid, {}).get("status") == "running":
        raise HTTPException(409, "rerun already in progress")
    _rerun_tasks[rid] = {"status": "running",
                         "started_at": datetime.utcnow().isoformat(), "tail": []}

    async def _run():
        import sys as _sys
        try:
            proc = await asyncio.create_subprocess_exec(
                _sys.executable, str(PROJECT_ROOT / spec["script"]),
                cwd=str(PROJECT_ROOT),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=1800)
            tail = out.decode(errors="replace").splitlines()[-40:]
            _rerun_tasks[rid].update(
                status="completed" if proc.returncode == 0 else "failed",
                returncode=proc.returncode, tail=tail,
                finished_at=datetime.utcnow().isoformat())
        except Exception as e:
            _rerun_tasks[rid].update(status="failed", tail=[f"{type(e).__name__}: {e}"])

    background_tasks.add_task(_run)
    return {"id": rid, "status": "running", "script": spec["script"]}


@router.get("/records/{rid}/rerun/status")
async def rerun_status(rid: str):
    if rid not in _rerun_tasks:
        return {"id": rid, "status": "idle"}
    return {"id": rid, **_rerun_tasks[rid]}


# ---------------------------------------------------------------------------
# Tear-sheet analytics + benchmark comparison
# ---------------------------------------------------------------------------

BENCH_DIR = get_data_dir() / "benchmarks"

# symbol -> loader. Local REAL files first; QQQ/ETH fetched once via yfinance
# and cached (still real market data — cached so analytics don't depend on
# network at request time).
_BENCH_LOCAL = {
    "SPY": ("data/sample/SPY_daily.csv", "close"),
    "BTC": ("data/sample/BTC_USDT_1d.csv", "close"),
}
_BENCH_YF = {"QQQ": "QQQ", "ETH": "ETH-USD"}
BENCHMARKS = list(_BENCH_LOCAL) + list(_BENCH_YF)


def _load_benchmark(symbol: str):
    import pandas as pd
    symbol = symbol.upper()
    if symbol in _BENCH_LOCAL:
        rel, col = _BENCH_LOCAL[symbol]
        df = pd.read_csv(PROJECT_ROOT / rel)
        tcol = "timestamp" if "timestamp" in df.columns else df.columns[0]
        df[tcol] = pd.to_datetime(df[tcol], utc=True).dt.tz_localize(None)
        return df.set_index(tcol)[col].astype(float)
    if symbol in _BENCH_YF:
        import pandas as pd
        BENCH_DIR.mkdir(parents=True, exist_ok=True)
        cache = BENCH_DIR / f"{symbol}_1d.csv"
        stale = (not cache.exists() or
                 (datetime.utcnow().timestamp() - cache.stat().st_mtime) > 7 * 86400)
        if stale:
            try:
                import yfinance as yf
                df = yf.download(_BENCH_YF[symbol], start="2015-01-01",
                                 progress=False, auto_adjust=True)
                if not df.empty:
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    df[["Close"]].rename(columns={"Close": "close"}).to_csv(cache)
            except Exception:
                pass
        if not cache.exists():
            raise HTTPException(502, f"benchmark fetch failed: {symbol}")
        df = pd.read_csv(cache, index_col=0, parse_dates=True)
        return df["close"].astype(float)
    raise HTTPException(404, f"unknown benchmark {symbol}; available: {BENCHMARKS}")


def _equity_series_from_result(result: Dict[str, Any]):
    import pandas as pd
    eq = result.get("equity_curve") or []
    if not eq:
        raise HTTPException(404, "result has no equity curve")
    ts = [e.get("timestamp") or e.get("ts") for e in eq]
    vals = [e.get("total_value", e.get("equity")) for e in eq]
    idx = pd.to_datetime(ts, utc=True, format="mixed").tz_localize(None)
    return pd.Series(vals, index=idx, dtype=float)


def _tearsheet_response(equity, benchmark_symbol):
    from backtesting.analysis.tearsheet import compute_tearsheet
    bench = _load_benchmark(benchmark_symbol) if benchmark_symbol else None
    return _sanitize(compute_tearsheet(equity, bench,
                                       benchmark_name=benchmark_symbol or ""))


@router.get("/benchmarks")
async def list_benchmarks():
    return {"benchmarks": BENCHMARKS}


@router.get("/results/{backtest_id}/analytics")
async def backtest_result_analytics(backtest_id: str,
                                    benchmark: Optional[str] = None):
    """Full tear sheet for an ad-hoc backtest run."""
    result = _backtest_store.get(backtest_id)
    if result is None:
        f = RESULTS_DIR / f"{backtest_id}.json"
        if not f.exists():
            raise HTTPException(404, f"backtest {backtest_id} not found")
        result = json.loads(f.read_text(encoding="utf-8"))
    return await asyncio.to_thread(
        _tearsheet_response, _equity_series_from_result(result), benchmark)


@router.get("/records/{rid}/analytics")
async def record_analytics(rid: str, benchmark: Optional[str] = None,
                           series: str = "combo_70_30"):
    """Full tear sheet for an official record (default: recommended combo)."""
    import pandas as pd
    spec = RECORD_MAP.get(rid)
    if spec and spec.get("curves"):
        df = pd.read_csv(REPO_RESULTS / spec["curves"], index_col=0,
                         parse_dates=True)
        if rid in ("combo_wheel_sniper", "combo_wheel_sniper_v2") and series == "combo_70_30":
            sn_col = "sniper" if "sniper" in df.columns else "sniper_honest"
            rw = df["wheel"].pct_change().fillna(0)
            rs = df[sn_col].pct_change().fillna(0)
            equity = 100_000 * (1 + 0.7 * rw + 0.3 * rs).cumprod()
        elif series in df.columns:
            equity = df[series].astype(float)
        else:
            raise HTTPException(404, f"series {series} not in record curves")
        return await asyncio.to_thread(_tearsheet_response, equity, benchmark)
    promoted = _promoted_record_file(rid)
    if promoted is not None:
        data = json.loads(promoted.read_text(encoding="utf-8"))
        return await asyncio.to_thread(
            _tearsheet_response, _equity_series_from_result(data), benchmark)
    raise HTTPException(404, "no curve data for this record")


# ---------------------------------------------------------------------------
# Promote an ad-hoc run to an official research record.
# A promoted run enters the registry as a CANDIDATE — the validated/rejected
# verdict still requires a proper IS/OOS research script (the 1.5 gate is
# never auto-granted from a single full-period run).
# ---------------------------------------------------------------------------

PROMOTED_DIR = PROJECT_ROOT / "results" / "records"


def _promoted_record_file(rid: str) -> Optional[Path]:
    f = PROMOTED_DIR / f"{rid}.json"
    return f if f.exists() else None


def _promoted_summaries() -> List[Dict[str, Any]]:
    out = []
    if not PROMOTED_DIR.exists():
        return out
    for f in sorted(PROMOTED_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        m = d.get("metrics", {})
        headline = {}
        for k, src in (("sharpe", "sharpe_ratio"), ("max_dd", "max_drawdown"),
                       ("cagr", "annual_return")):
            if m.get(src) is not None:
                headline[k] = m.get(src)
        out.append({
            "id": f.stem,
            "name": d.get("name", f.stem),
            "status": "candidate",
            "verdict": "CANDIDATE — needs IS/OOS validation before the 1.5 gate",
            "created": d.get("created_at"),
            "period": d.get("period"),
            "headline": headline,
            "data_note": d.get("data_file"),
            "script": None,
            "has_curve": bool(d.get("equity_curve")),
            "rerun": None,
            "notes": d.get("notes", ""),
        })
    return out


class PromoteRequest(BaseModel):
    name: Optional[str] = None
    notes: str = ""


@router.post("/results/{backtest_id}/promote")
async def promote_result(backtest_id: str, req: PromoteRequest):
    """Save an ad-hoc run into results/records/ as a candidate record."""
    result = _backtest_store.get(backtest_id)
    if result is None:
        f = RESULTS_DIR / f"{backtest_id}.json"
        if not f.exists():
            raise HTTPException(404, f"backtest {backtest_id} not found")
        result = json.loads(f.read_text(encoding="utf-8"))
    if result.get("status") != "completed":
        raise HTTPException(409, "only completed runs can be promoted")
    eq = result.get("equity_curve") or []
    rid = f"{result.get('strategy', 'run')}_{backtest_id[:8]}"
    record = {
        "name": req.name or f"{result.get('strategy')} {result.get('symbol')} run",
        "notes": req.notes,
        "strategy": result.get("strategy"),
        "symbol": result.get("symbol"),
        "created_at": result.get("created_at"),
        "promoted_at": datetime.utcnow().isoformat(),
        "parameters": result.get("parameters", {}),
        "metrics": result.get("metrics", {}),
        "data_file": result.get("data_file"),
        "period": {"start": (eq[0].get("timestamp") if eq else None),
                   "end": (eq[-1].get("timestamp") if eq else None)},
        "equity_curve": eq,
        "n_trades": result.get("n_trades"),
    }
    PROMOTED_DIR.mkdir(parents=True, exist_ok=True)
    (PROMOTED_DIR / f"{rid}.json").write_text(
        json.dumps(record, indent=1, default=str), encoding="utf-8")
    try:
        from data.quality.guards import record_data_manifest
        record_data_manifest([PROMOTED_DIR / f"{rid}.json"], f"promoted_{rid}")
    except Exception:
        pass
    return {"id": rid, "status": "candidate", "file": f"results/records/{rid}.json"}


# ---------------------------------------------------------------------------
# Runtime v2 backtests — same Strategy class as paper/live, over catalog
# data, results into the SAME store (status/results/analytics/tear-sheet
# endpoints all work unchanged).
# ---------------------------------------------------------------------------

@router.get("/v2/strategies")
async def v2_strategies():
    """Runtime strategies + their param schemas (drives the UI form)."""
    import strategies_v2
    out = []
    for sid, cls in strategies_v2.REGISTRY.items():
        out.append({
            "id": sid,
            "book_type": cls.book_type,
            "asset_domain": cls.asset_domain,
            "universe": list(cls.universe),
            "primary_tf": cls.primary_tf,
            "cadence_sec": cls.cadence_sec,
            "maker_only": bool(getattr(cls, "maker_only", False)),
            "params_schema": [
                {"name": s.name, "type": s.type, "default": s.default,
                 "min": s.min, "max": s.max, "step": s.step,
                 "choices": s.choices, "help": s.help}
                for s in cls.params_schema],
        })
    return {"strategies": out}


class V2RunRequest(BaseModel):
    strategy_id: str
    params: Dict[str, Any] = {}
    symbols: Optional[List[str]] = None      # default: strategy universe
    asset_class: str = "tokenized_stock"
    freq: Optional[str] = None               # default: strategy primary_tf
    initial_equity: float = 5_000.0
    start: Optional[str] = None
    end: Optional[str] = None


async def _run_v2_task(backtest_id: str, req: V2RunRequest):
    import pandas as pd
    try:
        _running_tasks[backtest_id] = "running"
        import strategies_v2
        from core.runtime.backtest_host import run_backtest as _rb
        from data.catalog_api import catalog

        cls = strategies_v2.REGISTRY[req.strategy_id]
        strat = cls(req.params)
        freq = req.freq or cls.primary_tf
        symbols = req.symbols or list(cls.universe)
        data = {}
        for sym in symbols:
            df = catalog.read(req.asset_class, sym, freq)
            df.columns = [c.lower() for c in df.columns]
            if req.start:
                df = df[df["timestamp"] >= pd.Timestamp(req.start)]
            if req.end:
                df = df[df["timestamp"] <= pd.Timestamp(req.end)]
            if len(df) > strat.warmup_bars() + 10:
                data[sym] = df.reset_index(drop=True)
        if not data:
            raise ValueError("no catalog data for requested symbols/freq")
        n = min(len(d) for d in data.values())
        data = {s: d.tail(n).reset_index(drop=True) for s, d in data.items()}

        import asyncio as _aio
        res = await _aio.to_thread(_rb, strat, data, req.initial_equity)

        record = _sanitize({
            "backtest_id": backtest_id, "engine": "runtime_v2",
            "strategy": req.strategy_id,
            "symbol": ",".join(symbols),
            "status": "completed",
            "created_at": datetime.utcnow().isoformat(),
            "initial_capital": req.initial_equity,
            "parameters": req.params,
            "data_file": f"catalog:{req.asset_class}/{freq} x{n}bars",
            "metrics": res["metrics"],
            "n_trades": res["n_trades"],
            "final_value": res["final_value"],
            "equity_curve": [{"timestamp": c["timestamp"],
                              "total_value": c["total_value"]}
                             for c in res["equity_curve"]],
            "trades": res["trades"][-500:],
        })
        _backtest_store[backtest_id] = record
        _running_tasks[backtest_id] = "completed"
        RESULTS_DIR.mkdir(exist_ok=True)
        with open(RESULTS_DIR / f"{backtest_id}.json", "w") as f:
            json.dump(record, f, indent=1, default=str)
    except Exception as e:
        logger.exception("v2 backtest failed")
        _backtest_store[backtest_id] = {
            "backtest_id": backtest_id, "strategy": req.strategy_id,
            "symbol": "", "status": "failed",
            "created_at": datetime.utcnow().isoformat(), "error": str(e),
            "metrics": {}, "n_trades": 0, "final_value": 0,
            "equity_curve": [], "trades": []}
        _running_tasks[backtest_id] = "failed"


@router.post("/v2/run")
async def v2_run(req: V2RunRequest, background_tasks: BackgroundTasks):
    import strategies_v2
    if req.strategy_id not in strategies_v2.REGISTRY:
        raise HTTPException(404, f"unknown v2 strategy {req.strategy_id}")
    backtest_id = f"v2_{uuid.uuid4().hex[:10]}"
    _running_tasks[backtest_id] = "queued"
    _backtest_store[backtest_id] = {
        "backtest_id": backtest_id, "strategy": req.strategy_id,
        "symbol": ",".join(req.symbols or []), "status": "running",
        "created_at": datetime.utcnow().isoformat(), "metrics": {},
        "n_trades": 0, "final_value": 0, "equity_curve": [], "trades": []}
    background_tasks.add_task(_run_v2_task, backtest_id, req)
    return {"backtest_id": backtest_id, "status": "submitted"}


class V2SweepRequest(BaseModel):
    strategy_id: str
    base_params: Dict[str, Any] = {}
    sweep: Dict[str, List[Any]]              # param -> values (1-2 params)
    symbols: Optional[List[str]] = None
    asset_class: str = "tokenized_stock"
    freq: Optional[str] = None
    initial_equity: float = 5_000.0


_sweeps: Dict[str, Dict[str, Any]] = {}


@router.post("/v2/sweep")
async def v2_sweep(req: V2SweepRequest, background_tasks: BackgroundTasks):
    """Grid sweep over 1-2 params. Runs sequentially in background."""
    import itertools
    import strategies_v2
    if req.strategy_id not in strategies_v2.REGISTRY:
        raise HTTPException(404, f"unknown v2 strategy {req.strategy_id}")
    if not (1 <= len(req.sweep) <= 2):
        raise HTTPException(400, "sweep supports 1-2 params")
    keys = list(req.sweep)
    combos = list(itertools.product(*req.sweep.values()))
    if len(combos) > 60:
        raise HTTPException(400, f"{len(combos)} combos > 60 cap")
    sweep_id = f"sw_{uuid.uuid4().hex[:8]}"
    _sweeps[sweep_id] = {"status": "running", "total": len(combos),
                         "done": 0, "results": [],
                         "strategy_id": req.strategy_id, "keys": keys}

    async def _run_all():
        import pandas as pd
        import asyncio as _aio
        from core.runtime.backtest_host import run_backtest as _rb
        from data.catalog_api import catalog
        cls = strategies_v2.REGISTRY[req.strategy_id]
        freq = req.freq or cls.primary_tf
        symbols = req.symbols or list(cls.universe)
        data = {}
        try:
            for sym in symbols:
                df = catalog.read(req.asset_class, sym, freq)
                df.columns = [c.lower() for c in df.columns]
                data[sym] = df.reset_index(drop=True)
            n = min(len(d) for d in data.values())
            data = {s: d.tail(n).reset_index(drop=True)
                    for s, d in data.items()}
            for vals in combos:
                params = {**req.base_params, **dict(zip(keys, vals))}
                try:
                    res = await _aio.to_thread(
                        _rb, cls(params), data, req.initial_equity)
                    _sweeps[sweep_id]["results"].append(_sanitize({
                        "params": dict(zip(keys, vals)),
                        **{k: res["metrics"].get(k) for k in
                           ("sharpe_ratio", "max_drawdown",
                            "total_return_pct", "total_trades",
                            "win_rate", "profit_factor")}}))
                except Exception as e:
                    _sweeps[sweep_id]["results"].append(
                        {"params": dict(zip(keys, vals)),
                         "error": str(e)[:80]})
                _sweeps[sweep_id]["done"] += 1
            _sweeps[sweep_id]["results"].sort(
                key=lambda r: r.get("sharpe_ratio") or -999, reverse=True)
            _sweeps[sweep_id]["status"] = "completed"
        except Exception as e:
            _sweeps[sweep_id]["status"] = "failed"
            _sweeps[sweep_id]["error"] = str(e)

    background_tasks.add_task(_run_all)
    return {"sweep_id": sweep_id, "combos": len(combos)}


@router.get("/v2/sweep/{sweep_id}")
async def v2_sweep_status(sweep_id: str):
    if sweep_id not in _sweeps:
        raise HTTPException(404, "unknown sweep")
    return _sweeps[sweep_id]
