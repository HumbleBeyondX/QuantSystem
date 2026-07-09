#!/usr/bin/env python3
"""Sweep the buy-vol signal threshold for Gamma Scalp.

Question (Part C): is the disabled buy-vol mode (buy_signal_threshold=0.80)
worth turning on? We sweep the threshold from 0.80 (off) down to 0.15
(aggressive long-vol) and compare Sharpe, return, DD, and the buy/sell PnL
split — to see whether catching vol spikes (like 2026-06-05) adds alpha or
just bleeds theta.

Reuses scripts/backtest_gamma_scalp.run_backtest_sync so the math matches the
live strategy exactly.
"""
from __future__ import annotations
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
from scripts.backtest_gamma_scalp import load_data, run_backtest_sync

DATA = str(ROOT / "data" / "sample" / "SPY_gamma_scalp_daily.csv")
CAPITAL = 100_000

# Buy-vol thresholds to test (0.80 = effectively off, lower = more long-vol trades)
BUY_THRESHOLDS = [0.80, 0.50, 0.40, 0.30, 0.25, 0.20, 0.15]
SELL_THRESHOLD = 0.25  # keep sell-vol mode fixed at default

def main():
    print("Loading data...")
    df = load_data(DATA)
    print(f"\nSweeping buy_signal_threshold (sell fixed at {SELL_THRESHOLD})...\n")

    rows = []
    for bt in BUY_THRESHOLDS:
        params = {
            "dte": 5,
            "base_contracts": 30,
            "sell_signal_threshold": SELL_THRESHOLD,
            "buy_signal_threshold": bt,
            "max_loss_pct": 0.20,
            "max_notional_pct": 0.45,
            "take_profit_pct": 0.70,
            "vix_floor": 12.0,
            "vix_ceiling": 40.0,
            "hedge_delta_threshold": 0.06,
        }
        res = run_backtest_sync(df, CAPITAL, params)
        m = res["metrics"]
        a = res["strategy_analytics"]
        rows.append({
            "buy_thr": bt,
            "sharpe": m.get("sharpe_ratio", 0),
            "ann_ret%": m.get("annual_return_pct", 0),
            "maxDD%": m.get("max_drawdown_pct", 0),
            "calmar": m.get("calmar_ratio", 0),
            "n_trades": a.get("total_trades", 0),
            "n_sell": a.get("sell_vol_trades", 0),
            "n_buy": a.get("buy_vol_trades", 0),
            "sell_pnl": a.get("sell_vol_pnl", 0),
            "buy_pnl": a.get("buy_vol_pnl", 0),
            "buy_win%": a.get("buy_win_rate", 0),
        })
        print(f"  buy_thr={bt:.2f}  Sharpe={rows[-1]['sharpe']:+.3f}  "
              f"AnnRet={rows[-1]['ann_ret%']:+6.2f}%  DD={rows[-1]['maxDD%']:6.2f}%  "
              f"trades={rows[-1]['n_trades']:3d} (sell {rows[-1]['n_sell']}/buy {rows[-1]['n_buy']})  "
              f"buyPnL=${rows[-1]['buy_pnl']:+,.0f}")

    out = pd.DataFrame(rows)
    print("\n" + "=" * 100)
    print("BUY-VOL THRESHOLD SWEEP — full table")
    print("=" * 100)
    print(out.to_string(index=False))
    print("=" * 100)

    # Verdict vs Sharpe ≥ 1.5 gate
    best = out.loc[out["sharpe"].idxmax()]
    print(f"\nBest Sharpe: {best['sharpe']:.3f} at buy_thr={best['buy_thr']:.2f}")
    print(f"User gate (Sharpe ≥ 1.5): "
          f"{'✅ PASS' if best['sharpe'] >= 1.5 else '❌ FAIL'}")

    # Isolate marginal value of buy-vol mode
    baseline = out[out["buy_thr"] == 0.80].iloc[0]
    print(f"\nBaseline (buy-vol OFF, thr=0.80): Sharpe {baseline['sharpe']:+.3f}, "
          f"AnnRet {baseline['ann_ret%']:+.2f}%")
    print(f"Marginal buy-vol PnL contribution by threshold:")
    for _, r in out.iterrows():
        if r["n_buy"] > 0:
            print(f"  thr={r['buy_thr']:.2f}: {r['n_buy']:3d} buy trades, "
                  f"net ${r['buy_pnl']:+,.0f}, win {r['buy_win%']:.0f}%  "
                  f"→ Sharpe {r['sharpe']:+.3f} (Δ vs base {r['sharpe']-baseline['sharpe']:+.3f})")

    csv_out = ROOT / "results" / "gamma_buyvol_sweep.csv"
    out.to_csv(csv_out, index=False)
    print(f"\nSaved → {csv_out}")

if __name__ == "__main__":
    main()
