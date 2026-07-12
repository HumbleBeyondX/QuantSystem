#!/usr/bin/env python3
"""C2 test: does the absolute-VRP override fix the 2-month-flat problem
WITHOUT hurting the Sharpe 1.98 edge?

Sweeps abs_vrp_weight (0.0 = current, blend up to 0.6 absolute) and the
z-score window. Reports per-config:
  - Sharpe / annual return / maxDD  (did the edge survive?)
  - n_trades + flat_days            (did it trade more, i.e. fix the flat gap?)
  - walk-forward OOS Sharpe         (is the improvement robust?)

Decision rule (test BEFORE we change defaults):
  Keep a config only if  OOS Sharpe ≥ 1.5  AND  it meaningfully reduces flat
  time vs baseline.
"""
from __future__ import annotations
import sys, warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
from scripts.backtest_gamma_scalp import load_data, run_backtest_sync

DATA = str(ROOT / "data" / "sample" / "SPY_gamma_scalp_daily.csv")
CAPITAL = 100_000
ANN = 252

BASE = {
    "dte": 5, "base_contracts": 30,
    "sell_signal_threshold": 0.25, "buy_signal_threshold": 0.80,
    "max_loss_pct": 0.20, "max_notional_pct": 0.45, "take_profit_pct": 0.70,
    "vix_floor": 12.0, "vix_ceiling": 40.0, "hedge_delta_threshold": 0.06,
}

# (label, overrides)
CONFIGS = [
    ("baseline (abs=0.0)",          {}),
    ("abs_w=0.2 ref1.5",            {"abs_vrp_weight": 0.2}),
    ("abs_w=0.3 ref1.5",            {"abs_vrp_weight": 0.3}),
    ("abs_w=0.4 ref1.5",            {"abs_vrp_weight": 0.4}),
    ("abs_w=0.3 ref1.3",            {"abs_vrp_weight": 0.3, "abs_vrp_ref": 1.3}),
    ("abs_w=0.3 ref1.8",            {"abs_vrp_weight": 0.3, "abs_vrp_ref": 1.8}),
    ("win=60 abs=0.0",             {"vrp_window": 60}),
    ("win=252 abs=0.0",            {"vrp_window": 252}),
    ("win=60 abs_w=0.3",           {"vrp_window": 60, "abs_vrp_weight": 0.3}),
]


def oos_sharpe(equity_curve, frac=0.6):
    eq = pd.DataFrame(equity_curve)
    eq["timestamp"] = pd.to_datetime(eq["timestamp"])
    eq = eq.set_index("timestamp").sort_index()
    rets = eq["total_value"].pct_change().dropna()
    split = int(len(rets) * frac)
    oos = rets.iloc[split:]
    is_ = rets.iloc[:split]
    def shp(r): return (r.mean()/r.std()*np.sqrt(ANN)) if r.std()>0 else 0.0
    return shp(is_), shp(oos)


def flat_days(equity_curve):
    eq = pd.DataFrame(equity_curve)
    return int((eq["n_positions"] == 0).sum()), len(eq)


def main():
    df = load_data(DATA)
    print(f"\nC2 sweep — absolute-VRP override (sell-vol entry fix)\n")
    rows = []
    for label, ov in CONFIGS:
        params = {**BASE, **ov}
        res = run_backtest_sync(df, CAPITAL, params)
        m = res["metrics"]; a = res["strategy_analytics"]
        is_s, oos_s = oos_sharpe(res["equity_curve"])
        fd, total = flat_days(res["equity_curve"])
        rows.append({
            "config": label,
            "sharpe": round(m.get("sharpe_ratio", 0), 3),
            "IS_shp": round(is_s, 3),
            "OOS_shp": round(oos_s, 3),
            "ann%": round(m.get("annual_return_pct", 0), 2),
            "DD%": round(m.get("max_drawdown_pct", 0), 2),
            "trades": a.get("total_trades", 0),
            "flat_d": fd,
            "flat%": round(fd / total * 100, 1),
        })
        print(f"  {label:22s}  Sharpe={rows[-1]['sharpe']:+.3f}  "
              f"OOS={rows[-1]['OOS_shp']:+.3f}  ann={rows[-1]['ann%']:+6.2f}%  "
              f"DD={rows[-1]['DD%']:5.2f}%  trades={rows[-1]['trades']:3d}  "
              f"flat={rows[-1]['flat%']:.0f}%")

    out = pd.DataFrame(rows)
    base = out[out["config"].str.startswith("baseline")].iloc[0]

    print("\n" + "=" * 100)
    print("C2 RESULTS — full table")
    print("=" * 100)
    print(out.to_string(index=False))
    print("=" * 100)

    print(f"\nBaseline: Sharpe {base['sharpe']:+.3f}, OOS {base['OOS_shp']:+.3f}, "
          f"flat {base['flat%']:.0f}%, {base['trades']} trades")
    print("\nConfigs that PASS (OOS≥1.5 AND fewer flat days than baseline):")
    any_pass = False
    for _, r in out.iterrows():
        if r["config"].startswith("baseline"):
            continue
        passed = r["OOS_shp"] >= 1.5 and r["flat_d"] < base["flat_d"]
        if passed:
            any_pass = True
            print(f"  ✅ {r['config']:22s}  OOS={r['OOS_shp']:+.3f}  "
                  f"flat {base['flat%']:.0f}%→{r['flat%']:.0f}%  "
                  f"({base['trades']}→{r['trades']} trades)  "
                  f"Sharpe Δ {r['sharpe']-base['sharpe']:+.3f}")
    if not any_pass:
        print("  (none — absolute-VRP override does NOT improve without breaking the edge)")

    csv_out = ROOT / "results" / "gamma_c2_sweep.csv"
    out.to_csv(csv_out, index=False)
    print(f"\nSaved → {csv_out}")


if __name__ == "__main__":
    main()
