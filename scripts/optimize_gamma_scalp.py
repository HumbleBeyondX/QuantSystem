"""Parameter optimizer for SPY Gamma Scalp Strategy.

Grid/random search over key parameters targeting Sharpe > 1.5.
"""

from __future__ import annotations

import itertools
import json
import math
import sys
import random
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.enums import AssetClass, TimeFrame
from core.models import Bar
from strategies.base import StrategyConfig
from strategies.registry import discover_strategies, create_strategy


def run_single(df: pd.DataFrame, initial_cash: float, params: dict) -> dict:
    """Run a single backtest and return key metrics."""
    from strategies.options.gamma_scalp_strategy import bs_straddle

    discover_strategies()

    p = dict(params)
    p["initial_capital"] = initial_cash

    config = StrategyConfig(
        strategy_id="gamma_scalp_opt",
        name="Gamma Scalp Opt",
        asset_class=AssetClass.EQUITY,
        symbols=["SPY"],
        parameters=p,
        mode="backtest",
    )
    strategy = create_strategy("spy_gamma_scalp", config)
    strategy.initialize()

    valid = df.dropna(subset=["close"]).copy()
    equity_values = []

    for idx, row in valid.iterrows():
        ts = row["timestamp"].to_pydatetime()
        spot = float(row["close"])

        metadata = {}
        if "implied_vol" in row and not pd.isna(row["implied_vol"]):
            metadata["implied_vol"] = float(row["implied_vol"])
        if "risk_free_rate" in row and not pd.isna(row["risk_free_rate"]):
            metadata["risk_free_rate"] = float(row["risk_free_rate"])
        if "vix_close" in row and not pd.isna(row["vix_close"]):
            metadata["vix_close"] = float(row["vix_close"])

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

        realized = strategy._running_pnl
        unrealized = 0.0
        if strategy._straddle_open and strategy._n_contracts > 0:
            T = strategy._time_to_expiry()
            iv = strategy._implied_vol
            rf = strategy._risk_free
            cp, pp = bs_straddle(spot, strategy._strike, T, rf, iv)
            n = strategy._n_contracts * strategy.multiplier
            sign = -1.0 if strategy._trade_direction == "sell_vol" else 1.0
            unrealized = ((cp + pp) - (strategy._entry_call_price + strategy._entry_put_price)) * n * sign
            unrealized += strategy._cum_hedge_pnl - strategy._cum_txn_costs

        equity_values.append(initial_cash + realized + unrealized)

    # Compute metrics
    eq = pd.Series(equity_values)
    returns = eq.pct_change().dropna()

    if len(returns) < 30:
        return {"sharpe": -99, "return_pct": 0, "max_dd": 0, "n_trades": 0, "vol": 0}

    ann_ret = (eq.iloc[-1] / eq.iloc[0]) ** (252 / len(eq)) - 1
    ann_vol = returns.std() * np.sqrt(252)
    sharpe = (ann_ret - 0.045) / ann_vol if ann_vol > 0 else 0

    # Max drawdown
    peak = eq.expanding().max()
    dd = (peak - eq) / peak
    max_dd = dd.max()

    # Sortino
    downside = returns[returns < 0]
    down_vol = downside.std() * np.sqrt(252) if len(downside) > 0 else 1e-6
    sortino = (ann_ret - 0.045) / down_vol

    n_trades = len(strategy._trade_results)
    win_rate = 0
    if n_trades > 0:
        win_rate = sum(1 for t in strategy._trade_results if t["total_pnl"] > 0) / n_trades

    return {
        "sharpe": round(sharpe, 4),
        "sortino": round(sortino, 4),
        "return_pct": round(ann_ret * 100, 2),
        "vol_pct": round(ann_vol * 100, 2),
        "max_dd_pct": round(max_dd * 100, 2),
        "n_trades": n_trades,
        "win_rate": round(win_rate * 100, 1),
        "total_pnl": round(eq.iloc[-1] - initial_cash, 2),
    }


def main():
    print("=" * 70)
    print("  GAMMA SCALP PARAMETER OPTIMIZER")
    print("=" * 70)

    data_path = ROOT / "data" / "sample" / "SPY_gamma_scalp_daily.csv"
    df = pd.read_csv(data_path, parse_dates=["timestamp"])
    print(f"  Data: {len(df)} rows ({df['timestamp'].iloc[0].date()} to {df['timestamp'].iloc[-1].date()})")

    initial_cash = 100_000

    # Parameter grid
    param_grid = {
        "dte": [5, 7],
        "base_contracts": [15, 20, 25, 30],
        "sell_signal_threshold": [0.15, 0.20, 0.25],
        "buy_signal_threshold": [0.80],  # Effectively disable buy vol
        "max_loss_pct": [0.20, 0.25, 0.30],
        "max_notional_pct": [0.25, 0.35, 0.45],
        "take_profit_pct": [0.60, 0.70, 0.80],
        "vix_floor": [11.0, 12.0],
        "vix_ceiling": [35.0, 40.0],
        "hedge_delta_threshold": [0.06, 0.10],
    }

    # Generate all combos
    keys = list(param_grid.keys())
    values = list(param_grid.values())
    all_combos = list(itertools.product(*values))
    print(f"\n  Total parameter combinations: {len(all_combos)}")

    # Random sample if too many
    max_samples = 200
    if len(all_combos) > max_samples:
        combos = random.sample(all_combos, max_samples)
        print(f"  Random sampling {max_samples} combos...")
    else:
        combos = all_combos

    results = []
    best_sharpe = -999
    best_params = None

    for i, combo in enumerate(combos):
        params = dict(zip(keys, combo))

        try:
            metrics = run_single(df, initial_cash, params)
        except Exception as e:
            continue

        metrics["params"] = params
        results.append(metrics)

        if metrics["sharpe"] > best_sharpe:
            best_sharpe = metrics["sharpe"]
            best_params = params

        if (i + 1) % 20 == 0:
            print(f"  [{i+1}/{len(combos)}] Best Sharpe so far: {best_sharpe:.4f}")

    # Sort by Sharpe
    results.sort(key=lambda x: x["sharpe"], reverse=True)

    print(f"\n{'='*70}")
    print(f"  TOP 10 PARAMETER SETS")
    print(f"{'='*70}")
    print(f"  {'#':>3} {'Sharpe':>8} {'Sortino':>8} {'Return%':>8} {'Vol%':>6} {'MaxDD%':>7} {'Trades':>7} {'WinR%':>6} {'PnL':>10}")
    print(f"  {'-'*3} {'-'*8} {'-'*8} {'-'*8} {'-'*6} {'-'*7} {'-'*7} {'-'*6} {'-'*10}")

    for i, r in enumerate(results[:10]):
        print(f"  {i+1:>3} {r['sharpe']:>8.3f} {r['sortino']:>8.3f} {r['return_pct']:>8.2f} "
              f"{r['vol_pct']:>6.2f} {r['max_dd_pct']:>7.2f} {r['n_trades']:>7} {r['win_rate']:>6.1f} "
              f"${r['total_pnl']:>9,.0f}")

    if best_params:
        print(f"\n  BEST PARAMETERS (Sharpe={best_sharpe:.4f}):")
        for k, v in best_params.items():
            print(f"    {k}: {v}")

    # Save
    output_path = ROOT / "results" / "gamma_scalp_optimization.json"
    with open(output_path, "w") as f:
        json.dump({"top_results": results[:20], "best_params": best_params}, f, indent=2)
    print(f"\n  Results saved to {output_path}")


if __name__ == "__main__":
    main()
