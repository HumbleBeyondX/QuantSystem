#!/usr/bin/env python3
"""ETH Momentum Sniper — optimized for ETH perpetual futures on 4H timeframe.

Based on BTC Sniper v5 architecture but with ETH-specific parameter optimization.
Comprehensive sweep across all key parameters at 50x leverage.

Usage:
    python scripts/backtest_eth_sniper.py
    python scripts/backtest_eth_sniper.py --sweep
    python scripts/backtest_eth_sniper.py --fine-tune
"""

from __future__ import annotations
import argparse, json, sys
from pathlib import Path
from typing import Any, Dict, List
from itertools import product
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import the core backtest engine and helpers from BTC script
from scripts.backtest_btc_sniper import (
    compute_signals,
    get_trend,
    BTCSniperBacktest,
    report,
)


# ════════════════════════════════════════════════════════════════
#  ETH-OPTIMIZED SWEEP CONFIGS
# ════════════════════════════════════════════════════════════════

def _eth_sweep_cfgs():
    """ETH-specific parameter sweep at 50x leverage.

    ETH characteristics vs BTC:
      - Higher volatility (larger ATR relative to price)
      - More mean-reverting on shorter timeframes
      - Stronger trend continuation in bull markets
      - More susceptible to liquidation cascades
    """
    configs = []

    # ── Group 1: Varying risk per trade (leverage fixed at 50x) ──
    for risk in [0.03, 0.05, 0.07, 0.10]:
        configs.append({
            "name": f"L50 R{int(risk*100)} S3.5",
            "leverage": 50, "risk_per_trade": risk,
            "atr_stop": 3.5, "atr_trail": 2.5,
            "atr_trail_activate": 2.0, "max_hold": 120,
        })

    # ── Group 2: Varying stop widths ──
    for stop, trail, trail_act in [
        (2.5, 2.0, 1.5),
        (3.0, 2.0, 1.5),
        (3.5, 2.5, 2.0),
        (4.0, 3.0, 2.0),
        (4.5, 3.0, 2.5),
        (5.0, 3.5, 2.5),
    ]:
        configs.append({
            "name": f"L50 S{stop} T{trail}",
            "leverage": 50, "risk_per_trade": 0.05,
            "atr_stop": stop, "atr_trail": trail,
            "atr_trail_activate": trail_act, "max_hold": 120,
        })

    # ── Group 3: Max hold period ──
    for hold in [60, 90, 120, 180, 240]:
        configs.append({
            "name": f"L50 Hold{hold}",
            "leverage": 50, "risk_per_trade": 0.05,
            "atr_stop": 3.5, "atr_trail": 2.5,
            "atr_trail_activate": 2.0, "max_hold": hold,
        })

    # ── Group 4: Entry selectivity ──
    for score in [2, 3, 4]:
        for vol_min in [1.0, 1.2, 1.5]:
            configs.append({
                "name": f"L50 Sc{score} V{vol_min}",
                "leverage": 50, "risk_per_trade": 0.05,
                "atr_stop": 3.5, "atr_trail": 2.5,
                "atr_trail_activate": 2.0, "max_hold": 120,
                "min_score": score, "vol_min": vol_min,
            })

    # ── Group 5: Margin cap ──
    for margin_pct in [0.20, 0.30, 0.40, 0.50]:
        configs.append({
            "name": f"L50 Margin{int(margin_pct*100)}",
            "leverage": 50, "risk_per_trade": 0.05,
            "atr_stop": 3.5, "atr_trail": 2.5,
            "max_margin_pct": margin_pct, "max_hold": 120,
        })

    # ── Group 6: Cooldown period ──
    for cd in [2, 3, 5, 8]:
        configs.append({
            "name": f"L50 CD{cd}",
            "leverage": 50, "risk_per_trade": 0.05,
            "atr_stop": 3.5, "atr_trail": 2.5,
            "cooldown": cd, "max_hold": 120,
        })

    # ── Group 7: Loss pause ──
    for pause_losses, pause_bars in [(3, 12), (5, 18), (7, 24), (3, 24)]:
        configs.append({
            "name": f"L50 PL{pause_losses}:{pause_bars}",
            "leverage": 50, "risk_per_trade": 0.05,
            "atr_stop": 3.5, "atr_trail": 2.5,
            "pause_after_losses": pause_losses, "pause_bars": pause_bars,
            "max_hold": 120,
        })

    # ── Group 8: Combined best candidates ──
    # Wider stops + longer holds (reduce liquidation)
    configs.extend([
        {"name": "ETH-A: WideStop",
         "leverage": 50, "risk_per_trade": 0.05,
         "atr_stop": 4.5, "atr_trail": 3.0,
         "atr_trail_activate": 2.5, "max_hold": 180,
         "min_score": 2, "vol_min": 1.2},
        {"name": "ETH-B: HighRisk",
         "leverage": 50, "risk_per_trade": 0.07,
         "atr_stop": 4.0, "atr_trail": 2.5,
         "atr_trail_activate": 2.0, "max_hold": 150,
         "min_score": 2, "vol_min": 1.0},
        {"name": "ETH-C: Selective",
         "leverage": 50, "risk_per_trade": 0.05,
         "atr_stop": 3.5, "atr_trail": 2.5,
         "atr_trail_activate": 2.0, "max_hold": 120,
         "min_score": 3, "vol_min": 1.5},
        {"name": "ETH-D: LongHold",
         "leverage": 50, "risk_per_trade": 0.05,
         "atr_stop": 4.0, "atr_trail": 3.0,
         "atr_trail_activate": 2.0, "max_hold": 240,
         "min_score": 2, "vol_min": 1.0},
        {"name": "ETH-E: AggroWide",
         "leverage": 50, "risk_per_trade": 0.07,
         "atr_stop": 4.5, "atr_trail": 3.0,
         "atr_trail_activate": 2.5, "max_hold": 180},
        {"name": "ETH-F: Conservative",
         "leverage": 50, "risk_per_trade": 0.03,
         "atr_stop": 4.0, "atr_trail": 3.0,
         "atr_trail_activate": 2.5, "max_hold": 180,
         "cooldown": 5},
    ])

    return configs


def _eth_fine_tune_cfgs():
    """Fine-tuning grid around the best params found in initial sweep."""
    configs = []

    # Grid search around the best performing configuration
    for risk in [0.04, 0.05, 0.06, 0.07, 0.08]:
        for stop in [3.0, 3.5, 4.0, 4.5]:
            for trail in [2.0, 2.5, 3.0]:
                for hold in [120, 150, 180]:
                    if trail >= stop:
                        continue  # trail must be < stop
                    trail_act = max(trail - 0.5, 1.0)
                    configs.append({
                        "name": f"R{int(risk*100)}S{stop}T{trail}H{hold}",
                        "leverage": 50,
                        "risk_per_trade": risk,
                        "atr_stop": stop,
                        "atr_trail": trail,
                        "atr_trail_activate": trail_act,
                        "max_hold": hold,
                    })

    return configs


def sweep_eth(df, capital, configs, label="ETH SNIPER"):
    """Run sweep and return sorted results."""
    print(f"\n{'='*150}")
    print(f"  {label} SWEEP ({len(configs)} configs) — 50x Leverage, Fixed Risk Sizing")
    print(f"{'='*150}")
    hdr = (f"  {'Config':<22s} {'Return':>8s} {'AnnRet':>8s} {'Sharpe':>8s} "
           f"{'Sortino':>8s} {'MaxDD':>8s} {'WinRate':>8s} {'PF':>6s} {'Trades':>7s} "
           f"{'AvgEqR%':>8s} {'Liqs':>6s} {'EndVal':>12s}")
    print(hdr)
    print(f"  {'-'*146}")

    all_r = []
    for cfg in configs:
        name = cfg.pop("name")
        bt = BTCSniperBacktest(initial_capital=capital, **cfg)
        res = bt.run(df)
        m = res.get("metrics", {})

        # Count liquidations
        exits = m.get("exits", {})
        liqs = exits.get("liquidation", 0)

        print(
            f"  {name:<22s} {m.get('total_return_pct',0):>+7.1f}% "
            f"{m.get('annual_return_pct',0):>+7.1f}% "
            f"{m.get('sharpe',0):>8.3f} "
            f"{m.get('sortino',0):>8.3f} "
            f"{m.get('max_dd_pct',0):>7.1f}% "
            f"{m.get('win_rate',0)*100:>7.1f}% "
            f"{m.get('profit_factor',0):>5.2f} "
            f"{m.get('trades',0):>7d} "
            f"{m.get('avg_equity_return_pct',0):>+7.2f}% "
            f"{liqs:>6d} "
            f"${m.get('end_eq',0):>11,.0f}"
        )
        all_r.append({"name": name, "liqs": liqs, **m})

    print(f"  {'-'*146}")

    # Sort by composite score: Sharpe + return, filter by constraints
    qualified = [r for r in all_r
                 if r.get("sharpe", 0) >= 1.0
                 and r.get("total_return_pct", 0) >= 2000]

    if qualified:
        print(f"\n  ✅ QUALIFIED (Sharpe ≥ 1.0 AND Return ≥ 2000%):")
        # Sort by Sharpe (primary), then by return
        qualified.sort(key=lambda x: (x.get("sharpe", 0), x.get("total_return_pct", 0)), reverse=True)
        for i, b in enumerate(qualified[:10]):
            liq_pct = b['liqs'] / max(b.get('trades', 1), 1) * 100
            print(f"    #{i+1}: {b['name']:<22s} -> "
                  f"Ret={b.get('total_return_pct',0):+.0f}%, "
                  f"Sharpe={b.get('sharpe',0):.3f}, "
                  f"Sortino={b.get('sortino',0):.3f}, "
                  f"DD={b.get('max_dd_pct',0):.1f}%, "
                  f"WR={b.get('win_rate',0)*100:.0f}%, "
                  f"PF={b.get('profit_factor',0):.2f}, "
                  f"Liqs={b['liqs']}/{b.get('trades',0)} ({liq_pct:.0f}%)")
    else:
        print(f"\n  ⚠️ No configs met both Sharpe ≥ 1.0 AND Return ≥ 2000%. Top 10 by Sharpe:")
        sorted_r = sorted(all_r, key=lambda x: x.get("sharpe", -999), reverse=True)
        for i, b in enumerate(sorted_r[:10]):
            liq_pct = b['liqs'] / max(b.get('trades', 1), 1) * 100
            print(f"    #{i+1}: {b['name']:<22s} -> "
                  f"Ret={b.get('total_return_pct',0):+.0f}%, "
                  f"Sharpe={b.get('sharpe',0):.3f}, "
                  f"DD={b.get('max_dd_pct',0):.1f}%, "
                  f"PF={b.get('profit_factor',0):.2f}, "
                  f"Liqs={b['liqs']}/{b.get('trades',0)} ({liq_pct:.0f}%)")
    print()
    return all_r


def main():
    parser = argparse.ArgumentParser(description="ETH Momentum Sniper — Optimization")
    parser.add_argument("--data", type=str, default=None)
    parser.add_argument("--capital", type=float, default=10000)
    parser.add_argument("--sweep", action="store_true", help="Run broad parameter sweep")
    parser.add_argument("--fine-tune", action="store_true", help="Run fine-tuning grid search")
    parser.add_argument("--save", type=str, default=None)
    args = parser.parse_args()

    data_file = args.data or str(PROJECT_ROOT / "data" / "sample" / "ETH_USDT_4h.csv")

    if not Path(data_file).exists():
        print(f"ERROR: No ETH data found at {data_file}")
        sys.exit(1)

    print(f"Loading: {data_file}")
    df = pd.read_csv(data_file)
    df.columns = [c.lower() for c in df.columns]
    for col in ("timestamp", "time", "datetime", "date"):
        if col in df.columns:
            df.rename(columns={col: "timestamp"}, inplace=True)
            break
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_localize(None)
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    print(f"Shape: {df.shape} | {df.timestamp.iloc[0]} -> {df.timestamp.iloc[-1]}\n")

    if args.fine_tune:
        configs = _eth_fine_tune_cfgs()
        all_r = sweep_eth(df, args.capital, configs, label="ETH FINE-TUNE")
    elif args.sweep:
        configs = _eth_sweep_cfgs()
        all_r = sweep_eth(df, args.capital, configs, label="ETH SNIPER")
    else:
        # Run baseline: same as BTC Sniper v5 with 50x leverage on ETH
        print("Running ETH baseline (BTC Sniper v5 params, 50x leverage)...")
        bt = BTCSniperBacktest(initial_capital=args.capital, leverage=50)
        res = bt.run(df)
        report(res, label="[ETH 50x Baseline]")
        all_r = [res.get("metrics", {})]

    if args.save:
        with open(args.save, "w") as f:
            json.dump(all_r, f, indent=2, default=str)
        print(f"Results saved to: {args.save}")


if __name__ == "__main__":
    main()
