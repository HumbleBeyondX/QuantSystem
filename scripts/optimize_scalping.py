#!/usr/bin/env python3
"""Parameter optimizer for the crypto scalping strategy.

Performs grid search or random search over strategy hyperparameters,
runs tick-level backtests for each combination, ranks by Sharpe ratio,
and saves results for dashboard viewing.

Usage:
    # Grid search with defaults
    python scripts/optimize_scalping.py --data data/sample/BTC_USDT_1m.csv

    # Quick random search (10 samples)
    python scripts/optimize_scalping.py --data data/sample/BTC_USDT_1m.csv --mode random --n-samples 10

    # Full grid search with custom params
    python scripts/optimize_scalping.py --data data/sample/BTC_USDT_trades.csv --tick-mode --initial-capital 50000
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backtesting.engine.event_driven import EventDrivenBacktest
from backtesting.engine.tick_replay import TickBacktestEngine
from backtesting.metrics.report import generate_text_report
from execution.models.fees import CryptoMakerTakerFee
from execution.models.slippage import FixedSlippage
from strategies.base import StrategyConfig
from strategies.registry import create_strategy, discover_strategies

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("optimize_scalping")

# ---------------------------------------------------------------------------
# Parameter search space
# ---------------------------------------------------------------------------

PARAM_GRID = {
    "ema_fast": [5, 7, 9, 12],
    "ema_slow": [15, 21, 30],
    "imbalance_threshold": [0.15, 0.25, 0.35, 0.45],
    "take_profit_pct": [0.15, 0.25, 0.35, 0.50],
    "stop_loss_pct": [0.08, 0.12, 0.18, 0.25],
    "cooldown_seconds": [10, 20, 30, 60],
}

# Constraints: ema_fast < ema_slow, take_profit > stop_loss
def is_valid_combo(params: Dict) -> bool:
    """Check parameter constraints."""
    if params["ema_fast"] >= params["ema_slow"]:
        return False
    if params["take_profit_pct"] <= params["stop_loss_pct"]:
        return False
    return True


# ---------------------------------------------------------------------------
# Backtest runner
# ---------------------------------------------------------------------------

async def run_single_backtest(
    params: Dict[str, Any],
    data: Dict[str, pd.DataFrame],
    initial_cash: float,
    tick_mode: bool = False,
) -> Dict[str, Any]:
    """Run a single backtest with given parameters.

    Returns dict with params + metrics.
    """
    discover_strategies()

    config = StrategyConfig(
        strategy_id=f"opt_{hash(frozenset(params.items())) % 10000:04d}",
        name="crypto_scalping",
        symbols=list(data.keys()),
        parameters=params,
        mode="backtest",
    )
    strategy = create_strategy("crypto_scalping", config)

    if tick_mode:
        engine = TickBacktestEngine(
            strategy=strategy,
            initial_cash=initial_cash,
            slippage=FixedSlippage(bps=2),
            fees=CryptoMakerTakerFee(maker=0.0002, taker=0.0005),
            eval_every_n=10,
            snapshot_every_n=100,
        )
    else:
        engine = EventDrivenBacktest(
            strategy=strategy,
            initial_cash=initial_cash,
            slippage=FixedSlippage(bps=2),
            fees=CryptoMakerTakerFee(maker=0.0002, taker=0.0005),
        )

    results = await engine.run(data)

    return {
        "params": params,
        "metrics": results.get("metrics", {}),
        "n_trades": results.get("n_trades", 0),
        "final_value": results.get("final_value", initial_cash),
        "equity_curve": results.get("equity_curve", []),
    }


# ---------------------------------------------------------------------------
# Grid / Random search
# ---------------------------------------------------------------------------

def generate_grid(param_grid: Dict[str, List]) -> List[Dict]:
    """Generate all valid parameter combinations."""
    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combos = []
    for vals in itertools.product(*values):
        params = dict(zip(keys, vals))
        if is_valid_combo(params):
            combos.append(params)
    return combos


def generate_random_samples(param_grid: Dict[str, List], n: int, seed: int = 42) -> List[Dict]:
    """Generate random parameter combinations."""
    rng = np.random.RandomState(seed)
    samples = []
    attempts = 0
    while len(samples) < n and attempts < n * 10:
        params = {k: rng.choice(v) for k, v in param_grid.items()}
        # Convert numpy types to Python types
        params = {k: int(v) if isinstance(v, (np.integer,)) else float(v) for k, v in params.items()}
        if is_valid_combo(params) and params not in samples:
            samples.append(params)
        attempts += 1
    return samples


async def run_optimization(
    combos: List[Dict],
    data: Dict[str, pd.DataFrame],
    initial_cash: float,
    tick_mode: bool = False,
) -> List[Dict]:
    """Run all parameter combinations and collect results."""
    results = []
    total = len(combos)

    for i, params in enumerate(combos):
        logger.info("=" * 50)
        logger.info("Run %d/%d: %s", i + 1, total, params)
        logger.info("=" * 50)

        start_time = time.time()
        try:
            result = await run_single_backtest(params, data, initial_cash, tick_mode)
            elapsed = time.time() - start_time

            sharpe = result["metrics"].get("sharpe_ratio", 0)
            total_ret = result["metrics"].get("total_return_pct", 0)
            n_trades = result["n_trades"]

            logger.info(
                "  Result: Sharpe=%.2f  Return=%.2f%%  Trades=%d  (%.1fs)",
                sharpe, total_ret, n_trades, elapsed,
            )

            results.append(result)
        except Exception as e:
            logger.error("  Failed: %s", e)
            results.append({"params": params, "metrics": {}, "n_trades": 0, "final_value": initial_cash, "error": str(e)})

    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def rank_results(results: List[Dict]) -> pd.DataFrame:
    """Rank results by Sharpe ratio."""
    rows = []
    for r in results:
        m = r.get("metrics", {})
        row = {
            **r["params"],
            "sharpe": m.get("sharpe_ratio", -999),
            "return_pct": m.get("total_return_pct", 0),
            "max_dd_pct": m.get("max_drawdown_pct", 0),
            "win_rate": m.get("win_rate", 0),
            "profit_factor": m.get("profit_factor", 0),
            "sortino": m.get("sortino_ratio", 0),
            "n_trades": r.get("n_trades", 0),
            "final_value": r.get("final_value", 0),
        }
        rows.append(row)

    df = pd.DataFrame(rows)
    df = df.sort_values("sharpe", ascending=False).reset_index(drop=True)
    df.index.name = "rank"
    return df


def print_top_results(ranking: pd.DataFrame, top_n: int = 10):
    """Print top N results."""
    print("\n" + "=" * 80)
    print(f"  TOP {min(top_n, len(ranking))} PARAMETER COMBINATIONS (by Sharpe Ratio)")
    print("=" * 80)

    cols = ["ema_fast", "ema_slow", "imbalance_threshold", "take_profit_pct",
            "stop_loss_pct", "cooldown_seconds", "sharpe", "return_pct", "max_dd_pct",
            "win_rate", "n_trades"]

    display_cols = [c for c in cols if c in ranking.columns]
    print(ranking.head(top_n)[display_cols].to_string())

    print("\n--- Best Parameters ---")
    if len(ranking) > 0:
        best = ranking.iloc[0]
        print(f"  EMA Fast:         {int(best.get('ema_fast', 0))}")
        print(f"  EMA Slow:         {int(best.get('ema_slow', 0))}")
        print(f"  Imbalance Thresh: {best.get('imbalance_threshold', 0):.2f}")
        print(f"  Take Profit:      {best.get('take_profit_pct', 0):.2f}%")
        print(f"  Stop Loss:        {best.get('stop_loss_pct', 0):.2f}%")
        print(f"  Cooldown:         {int(best.get('cooldown_seconds', 0))}s")
        print(f"  Sharpe:           {best.get('sharpe', 0):.3f}")
        print(f"  Return:           {best.get('return_pct', 0):.2f}%")
        print(f"  Max Drawdown:     {best.get('max_dd_pct', 0):.2f}%")
        print(f"  Win Rate:         {best.get('win_rate', 0):.2%}")
        print(f"  Trades:           {int(best.get('n_trades', 0))}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_data(path: str) -> Dict[str, pd.DataFrame]:
    """Load data from CSV file(s)."""
    p = Path(path)
    if p.is_file():
        symbol = p.stem.replace("_", "/")
        # Fix common patterns like BTC_USDT_1m -> BTC/USDT
        parts = symbol.split("/")
        if len(parts) >= 2:
            symbol = f"{parts[0]}/{parts[1]}"
        df = pd.read_csv(p, parse_dates=["timestamp"] if "timestamp" in pd.read_csv(p, nrows=0).columns else True)
        # Normalize column names
        df.columns = [c.lower() for c in df.columns]
        return {symbol: df}
    elif p.is_dir():
        data = {}
        for f in sorted(p.glob("*.csv")):
            symbol = f.stem.replace("_", "/").split("/")
            sym = f"{symbol[0]}/{symbol[1]}" if len(symbol) >= 2 else symbol[0]
            df = pd.read_csv(f, parse_dates=True)
            df.columns = [c.lower() for c in df.columns]
            data[sym] = df
        return data
    else:
        logger.error("Data path not found: %s", path)
        sys.exit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Optimize crypto scalping strategy")
    parser.add_argument("--data", required=True, help="Path to CSV data file or directory")
    parser.add_argument("--mode", default="grid", choices=["grid", "random"])
    parser.add_argument("--n-samples", type=int, default=20, help="Random search samples (default: 20)")
    parser.add_argument("--initial-capital", type=float, default=100_000)
    parser.add_argument("--tick-mode", action="store_true", help="Use tick-level backtest engine")
    parser.add_argument("--output", default=None, help="Output JSON path")
    return parser.parse_args()


async def _async_main(args: argparse.Namespace):
    data = load_data(args.data)
    if not data:
        logger.error("No data loaded")
        sys.exit(1)

    for sym, df in data.items():
        logger.info("Loaded %d rows for %s", len(df), sym)

    # Generate parameter combinations
    if args.mode == "grid":
        combos = generate_grid(PARAM_GRID)
        logger.info("Grid search: %d valid combinations", len(combos))
    else:
        combos = generate_random_samples(PARAM_GRID, args.n_samples)
        logger.info("Random search: %d samples", len(combos))

    # Run optimization
    results = await run_optimization(combos, data, args.initial_capital, args.tick_mode)

    # Rank and display
    ranking = rank_results(results)
    print_top_results(ranking)

    # Save results
    output_path = args.output
    if output_path is None:
        results_dir = PROJECT_ROOT / "results"
        results_dir.mkdir(exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        output_path = str(results_dir / f"scalping_optimization_{ts}.json")

    # Save full results (without equity curves for space)
    save_results = []
    for r in results:
        save_r = {k: v for k, v in r.items() if k != "equity_curve"}
        save_results.append(save_r)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.utcnow().isoformat(),
            "n_combinations": len(combos),
            "mode": args.mode,
            "initial_capital": args.initial_capital,
            "ranking": ranking.to_dict(orient="records"),
            "all_results": save_results,
        }, f, indent=2, default=str)

    logger.info("Results saved to %s", output_path)

    # Also save best params as YAML for easy use
    if len(ranking) > 0:
        best = ranking.iloc[0]
        best_yaml_path = PROJECT_ROOT / "config" / "strategies" / "crypto_scalping_optimized.yaml"
        with open(best_yaml_path, "w") as f:
            f.write(f"""# Auto-generated optimized parameters
# Generated: {datetime.utcnow().isoformat()}
# Sharpe: {best.get('sharpe', 0):.3f} | Return: {best.get('return_pct', 0):.2f}%

strategy_id: crypto_scalping_optimized
name: Crypto Scalping (Optimized)
asset_class: crypto
enabled: true
mode: paper

symbols:
  - BTC/USDT
  - ETH/USDT

timeframe: 1s

parameters:
  ema_fast: {int(best.get('ema_fast', 9))}
  ema_slow: {int(best.get('ema_slow', 21))}
  rsi_period: 14
  rsi_overbought: 70
  rsi_oversold: 30
  imbalance_threshold: {best.get('imbalance_threshold', 0.3):.2f}
  take_profit_pct: {best.get('take_profit_pct', 0.3):.2f}
  stop_loss_pct: {best.get('stop_loss_pct', 0.15):.2f}
  cooldown_seconds: {int(best.get('cooldown_seconds', 30))}
  max_position_size_usd: 10000

risk:
  max_position_pct: 0.10
  max_daily_loss_pct: 0.02
  max_open_positions: 2
""")
        logger.info("Best params saved to %s", best_yaml_path)


def main():
    args = parse_args()
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
