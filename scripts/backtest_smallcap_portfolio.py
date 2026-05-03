#!/usr/bin/env python3
"""Run a multi-ticker portfolio backtest of the small-cap mean-reversion
strategy. Each ticker gets equal allocated capital and runs independently;
combined equity curve is the sum.

The diversification effect (signals fire on different days across tickers)
should improve Sharpe even if individual tickers are only marginal.

Usage:
    python scripts/backtest_smallcap_portfolio.py --tickers XBI GDX UAL KRE
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.smallcap_mean_reversion import run_smallcap_mr_backtest  # noqa: E402


def fetch_daily(ticker: str, start: str, end: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.Ticker(ticker).history(start=start, end=end, interval="1d", auto_adjust=True)
    if df.empty:
        return pd.DataFrame()
    df = df.reset_index()
    df.columns = [c.lower().replace(" ", "_") for c in df.columns]
    if "timestamp" not in df.columns and "date" in df.columns:
        df = df.rename(columns={"date": "timestamp"})
    ts = pd.to_datetime(df["timestamp"])
    if hasattr(ts.dt, "tz") and ts.dt.tz is not None:
        ts = ts.dt.tz_localize(None)
    df["timestamp"] = ts
    return df.sort_values("timestamp").reset_index(drop=True)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", nargs="+", default=["XBI", "GDX", "UAL", "KRE"])
    p.add_argument("--total-capital", type=float, default=10_000)
    p.add_argument("--drop", type=float, default=2.5)
    p.add_argument("--rsi-os", type=float, default=20.0)
    p.add_argument("--pt", type=float, default=1.5)
    p.add_argument("--sl", type=float, default=2.5)
    p.add_argument("--max-hold", type=int, default=4)
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--vix-max", type=float, default=999.0,
                   help="Skip entries when VIX > this (default 999=disabled)")
    p.add_argument("--zscore-sizing", action="store_true",
                   help="Scale position by extremeness of oversold")
    p.add_argument("--weight", choices=["equal", "inverse-vol", "sharpe"],
                   default="equal",
                   help="Sleeve weighting: equal / inverse-vol / sharpe-weighted")
    p.add_argument("--leverage", type=float, default=1.0,
                   help="Margin leverage (1.0=cash only, 5.0=5× margin)")
    p.add_argument("--borrow-rate", type=float, default=0.06,
                   help="Annual borrow cost on margin loan")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    n = len(args.tickers)
    print(f"Portfolio backtest: {n} sleeves, weighting={args.weight}, "
          f"total ${args.total_capital:,.0f}")
    print(f"Period: {args.start} → {args.end}\n")

    # Fetch VIX once for all sleeves
    vix_series = None
    if args.vix_max < 999:
        print(f"  Fetching VIX...", end="", flush=True)
        vix_df = fetch_daily("^VIX", args.start, args.end)
        if not vix_df.empty:
            vix_series = vix_df.set_index("timestamp")["close"]
            print(f" {len(vix_series)} bars, range {vix_series.min():.1f}-{vix_series.max():.1f}")
        else:
            print(" FAILED, disabling VIX filter")
            args.vix_max = 999

    # ── Pre-pass: estimate per-ticker vol/Sharpe with equal weight ──
    # to compute weights for sharpe / inverse-vol modes
    weights = {}
    if args.weight == "equal":
        weights = {t: 1.0 / n for t in args.tickers}
    else:
        print("Pre-pass for weighting...")
        equal_per_sleeve = args.total_capital / n
        prelim = {}
        for t in args.tickers:
            df = fetch_daily(t, args.start, args.end)
            if df.empty or len(df) < 220:
                continue
            r = run_smallcap_mr_backtest(
                t, df, initial_capital=equal_per_sleeve,
                drop_threshold_pct=args.drop, rsi2_oversold=args.rsi_os,
                profit_target_pct=args.pt, stop_loss_pct=args.sl,
                max_hold_days=args.max_hold,
            )
            m = r.metrics()
            prelim[t] = m
        # Compute raw weights
        if args.weight == "inverse-vol":
            raw = {t: 1.0 / max(m.get("annual_volatility", 0.01), 0.001)
                   for t, m in prelim.items() if m.get("annual_volatility", 0) > 0}
        else:  # sharpe
            raw = {t: max(m.get("sharpe_ratio", 0), 0.01) for t, m in prelim.items()}
        total_raw = sum(raw.values()) or 1.0
        weights = {t: v / total_raw for t, v in raw.items()}

    print(f"\nWeights: { {t: round(w, 3) for t, w in weights.items()} }\n")

    sleeves: dict = {}
    all_equity_dfs = []
    for t in args.tickers:
        if t not in weights:
            continue
        per_sleeve = args.total_capital * weights[t]
        print(f"  {t} (alloc ${per_sleeve:,.0f})...", end="", flush=True)
        df = fetch_daily(t, args.start, args.end)
        if df.empty or len(df) < 220:
            print(f" SKIP")
            continue
        result = run_smallcap_mr_backtest(
            t, df,
            initial_capital=per_sleeve,
            drop_threshold_pct=args.drop,
            rsi2_oversold=args.rsi_os,
            profit_target_pct=args.pt,
            stop_loss_pct=args.sl,
            max_hold_days=args.max_hold,
            vix_series=vix_series,
            vix_max=args.vix_max,
            zscore_sizing=args.zscore_sizing,
            leverage=args.leverage,
            annual_borrow_rate=args.borrow_rate,
        )
        m = result.metrics()
        sleeves[t] = m
        print(f" {len(df)} bars → trades={m['n_trades']}, "
              f"win={m['win_rate_pct']}%, Sharpe={m['sharpe_ratio']}, "
              f"final=${m['final_value']:,.0f}")
        eq_df = pd.DataFrame(result.equity_curve, columns=["ts", t]).set_index("ts")
        all_equity_dfs.append(eq_df)

    if not sleeves:
        print("No sleeves completed.")
        return 1

    # ── Combine equity curves ──
    combined = pd.concat(all_equity_dfs, axis=1).ffill().fillna(0)
    combined["total"] = combined.sum(axis=1)
    combined = combined.dropna(subset=["total"])
    combined["ret"] = combined["total"].pct_change().fillna(0)

    initial = combined["total"].iloc[0]
    final = combined["total"].iloc[-1]
    n_days = (combined.index[-1] - combined.index[0]).days
    years = n_days / 365.25
    total_return = final / initial - 1
    ann_return = (1 + total_return) ** (1 / years) - 1
    ann_vol = combined["ret"].std() * np.sqrt(252)
    sharpe = (ann_return / ann_vol) if ann_vol > 0 else 0.0

    running_max = combined["total"].cummax()
    dd = (combined["total"] / running_max - 1).min()

    total_trades = sum(s["n_trades"] for s in sleeves.values())
    weighted_win = sum(s["n_trades"] * s["win_rate_pct"] for s in sleeves.values()) / max(total_trades, 1)

    print("\n" + "=" * 80)
    print("PORTFOLIO COMBINED METRICS")
    print("=" * 80)
    print(f"  Tickers:           {', '.join(sleeves.keys())}")
    print(f"  Initial:           ${initial:,.0f}")
    print(f"  Final:             ${final:,.0f}")
    print(f"  Total return:      {total_return * 100:+.2f}%")
    print(f"  Annual return:     {ann_return * 100:+.2f}%")
    print(f"  Annual volatility: {ann_vol * 100:.2f}%")
    print(f"  Sharpe ratio:      {sharpe:.3f}")
    print(f"  Max drawdown:      {abs(dd) * 100:.2f}%")
    print(f"  Total trades:      {total_trades}")
    print(f"  Weighted win rate: {weighted_win:.1f}%")
    print()

    # Verdict
    if sharpe >= 1.5 and abs(dd) <= 0.15 and weighted_win >= 65:
        verdict = "✅ EXCELLENT — meets all targets"
    elif sharpe >= 1.0 and abs(dd) <= 0.20 and weighted_win >= 60:
        verdict = "✅ STRONG — usable for live deployment"
    elif sharpe >= 0.7:
        verdict = "🟡 OK — needs more diversification or parameter tuning"
    elif sharpe > 0:
        verdict = "🟠 MARGINAL"
    else:
        verdict = "❌ POOR"
    print(f"  Verdict: {verdict}")
    print("=" * 80)

    out = PROJECT_ROOT / "results" / f"smallcap_portfolio_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    with open(out, "w") as f:
        json.dump({
            "config": vars(args),
            "sleeves": sleeves,
            "portfolio": {
                "initial": initial, "final": final,
                "total_return_pct": total_return * 100,
                "annual_return_pct": ann_return * 100,
                "annual_volatility": ann_vol,
                "sharpe_ratio": sharpe,
                "max_drawdown_pct": abs(dd) * 100,
                "total_trades": total_trades,
                "weighted_win_rate_pct": weighted_win,
            },
        }, f, indent=2, default=str)
    print(f"\n→ {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
