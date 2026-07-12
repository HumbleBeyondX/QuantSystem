#!/usr/bin/env python3
"""Walk-forward / out-of-sample validation for Gamma Scalp sell-vol mode.

Part D: is the Sharpe 1.98 (full-sample, buy-vol OFF) a real, regime-stable
edge — or an artifact of one lucky period?

Method:
  1. Run the full backtest ONCE with the production params (buy-vol off).
  2. Take the daily equity curve, compute daily returns.
  3. Slice returns into:
       - calendar years (2023, 2024, 2025, 2026-partial)
       - IS (first 60%) vs OOS (last 40%)
  4. Report Sharpe / annualized return / maxDD per slice.

Because params are FIXED (not fitted), consistent Sharpe across slices =
genuine edge; a Sharpe that lives in one slice only = regime luck.
"""
from __future__ import annotations
import sys
import warnings
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

# Production params — the Sharpe 1.98 config (buy-vol disabled)
PROD_PARAMS = {
    "dte": 5,
    "base_contracts": 30,
    "sell_signal_threshold": 0.25,
    "buy_signal_threshold": 0.80,   # OFF
    "max_loss_pct": 0.20,
    "max_notional_pct": 0.45,
    "take_profit_pct": 0.70,
    "vix_floor": 12.0,
    "vix_ceiling": 40.0,
    "hedge_delta_threshold": 0.06,
}


def slice_metrics(returns: pd.Series, label: str) -> dict:
    """Sharpe / ann return / maxDD / vol on a daily-return slice."""
    r = returns.dropna()
    if len(r) < 20:
        return {"window": label, "n_days": len(r), "sharpe": np.nan,
                "ann_ret%": np.nan, "ann_vol%": np.nan, "maxDD%": np.nan}
    mean_d = r.mean()
    std_d = r.std()
    sharpe = (mean_d / std_d * np.sqrt(ANN)) if std_d > 0 else 0.0
    ann_ret = mean_d * ANN
    ann_vol = std_d * np.sqrt(ANN)
    # Max drawdown from cumulative
    equity = (1 + r).cumprod()
    dd = (equity / equity.cummax() - 1).min()
    return {
        "window": label,
        "n_days": len(r),
        "sharpe": round(sharpe, 3),
        "ann_ret%": round(ann_ret * 100, 2),
        "ann_vol%": round(ann_vol * 100, 2),
        "maxDD%": round(abs(dd) * 100, 2),
    }


def main():
    print("Loading data...")
    df = load_data(DATA)
    print("\nRunning full backtest with production params (buy-vol OFF)...")
    res = run_backtest_sync(df, CAPITAL, PROD_PARAMS)

    full_sharpe = res["metrics"].get("sharpe_ratio", 0)
    print(f"\nFull-sample Sharpe (reference): {full_sharpe:+.3f}")

    # Build daily-return series with datetime index
    eq = pd.DataFrame(res["equity_curve"])
    eq["timestamp"] = pd.to_datetime(eq["timestamp"])
    eq = eq.set_index("timestamp").sort_index()
    eq["ret"] = eq["total_value"].pct_change()
    rets = eq["ret"]

    print("\n" + "=" * 88)
    print("WALK-FORWARD — Sharpe stability across regimes (params FIXED)")
    print("=" * 88)

    slices = []

    # ── By calendar year ──
    for yr in sorted(eq.index.year.unique()):
        yr_rets = rets[eq.index.year == yr]
        slices.append(slice_metrics(yr_rets, f"Year {yr}"))

    # ── IS / OOS 60-40 split ──
    n = len(rets)
    split = int(n * 0.6)
    is_rets = rets.iloc[:split]
    oos_rets = rets.iloc[split:]
    is_start, is_end = eq.index[0].date(), eq.index[split-1].date()
    oos_start, oos_end = eq.index[split].date(), eq.index[-1].date()
    slices.append(slice_metrics(is_rets, f"IS  60% ({is_start}→{is_end})"))
    slices.append(slice_metrics(oos_rets, f"OOS 40% ({oos_start}→{oos_end})"))

    out = pd.DataFrame(slices)
    print(out.to_string(index=False))
    print("=" * 88)

    # ── Verdict ──
    oos_row = out[out["window"].str.startswith("OOS")].iloc[0]
    is_row = out[out["window"].str.startswith("IS")].iloc[0]
    yearly = out[out["window"].str.startswith("Year")]

    print(f"\nIS  Sharpe: {is_row['sharpe']:+.3f}")
    print(f"OOS Sharpe: {oos_row['sharpe']:+.3f}")
    degradation = (is_row["sharpe"] - oos_row["sharpe"]) / abs(is_row["sharpe"]) * 100 \
        if is_row["sharpe"] else 0
    print(f"OOS degradation: {degradation:+.1f}%  "
          f"({'acceptable' if abs(degradation) < 40 else 'LARGE — possible overfit'})")

    n_years_pass = (yearly["sharpe"] >= 1.5).sum()
    print(f"\nYears with Sharpe ≥ 1.5: {n_years_pass}/{len(yearly)}")
    print(f"Worst year Sharpe: {yearly['sharpe'].min():+.3f}")

    print("\nVERDICT:")
    if oos_row["sharpe"] >= 1.5 and abs(degradation) < 40:
        print(f"  ✅ OOS Sharpe {oos_row['sharpe']:.2f} ≥ 1.5 AND stable → edge is REAL")
        print("     → proceed to C2 (fix the 2-month-flat sell-vol filter)")
    elif oos_row["sharpe"] >= 1.0:
        print(f"  ⚠️  OOS Sharpe {oos_row['sharpe']:.2f} positive but < 1.5 → edge exists, weaker OOS")
        print("     → C2 worth testing but manage expectations")
    else:
        print(f"  ❌ OOS Sharpe {oos_row['sharpe']:.2f} < 1.0 → full-sample 1.98 likely regime luck")
        print("     → do NOT invest in C2 until edge is re-established")

    csv_out = ROOT / "results" / "gamma_walkforward.csv"
    out.to_csv(csv_out, index=False)
    print(f"\nSaved → {csv_out}")


if __name__ == "__main__":
    main()
