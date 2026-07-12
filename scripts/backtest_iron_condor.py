#!/usr/bin/env python3
"""Iron Condor backtest CLI — runs across one or more tickers, prints comparison.

Usage:
    python scripts/backtest_iron_condor.py --tickers SPY QQQ IWM RIOT INTC
    python scripts/backtest_iron_condor.py --tickers SPY --short-delta 0.20 --pt 0.25
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import List

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.iron_condor_backtest import run_iron_condor_backtest  # noqa: E402


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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", nargs="+", required=True)
    p.add_argument("--capital", type=float, default=10_000)
    p.add_argument("--short-delta", type=float, default=0.16,
                   help="Short strike delta (0.16 ≈ 1σ OTM, ~70%% prob OTM)")
    p.add_argument("--wing-mult", type=float, default=1.0,
                   help="Wing width = N × 1-month σ-move from spot")
    p.add_argument("--dte", type=int, default=30)
    p.add_argument("--pt", type=float, default=0.50,
                   help="Profit target as fraction of credit (0.50 = close at 50%% max profit)")
    p.add_argument("--sl", type=float, default=2.0,
                   help="Stop loss as multiple of credit (2.0 = exit when loss = 2× credit)")
    p.add_argument("--max-risk", type=float, default=0.50,
                   help="Max %% of capital risked at once")
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--vrp", type=float, default=1.15, help="IV/RV multiplier")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    print(f"Iron Condor backtest")
    print(f"  Period:        {args.start} → {args.end}")
    print(f"  Capital:       ${args.capital:,.0f}")
    print(f"  Short delta:   {args.short_delta}")
    print(f"  Wing mult:     {args.wing_mult}σ")
    print(f"  DTE:           {args.dte}")
    print(f"  Profit target: {args.pt * 100:.0f}% of credit")
    print(f"  Stop loss:     {args.sl}× credit")
    print(f"  Max risk:      {args.max_risk * 100:.0f}% of capital\n")

    rows = []
    full_results = {}
    for ticker in args.tickers:
        print(f"  {ticker}...", end="", flush=True)
        df = fetch_daily(ticker, args.start, args.end)
        if df.empty or len(df) < 60:
            print(f" SKIP ({len(df)} bars)")
            continue
        print(f" {len(df)} bars, ${df['close'].iloc[0]:.2f} → ${df['close'].iloc[-1]:.2f}")

        result = run_iron_condor_backtest(
            ticker, df,
            initial_capital=args.capital,
            short_delta=args.short_delta,
            wing_width_atr_mult=args.wing_mult,
            target_dte=args.dte,
            vrp_multiplier=args.vrp,
            profit_target_pct=args.pt,
            stop_loss_mult=args.sl,
            max_risk_pct=args.max_risk,
        )
        m = result.metrics()
        rows.append(m)
        full_results[ticker] = {
            "metrics": m,
            "trades": [asdict(t) for t in result.trades],
        }
        time.sleep(0.2)

    if not rows:
        print("No backtests completed.")
        return 1

    df_summary = pd.DataFrame(rows)
    print("\n" + "=" * 120)
    print("IRON CONDOR RESULTS (sorted by Sharpe)")
    print("=" * 120)
    df_summary = df_summary.sort_values("sharpe_ratio", ascending=False)
    cols = [
        "ticker", "n_trades", "win_rate_pct", "profit_target_hits",
        "stop_loss_hits", "expirations", "wing_breaks",
        "final_value", "total_return_pct", "annual_return_pct",
        "sharpe_ratio", "max_drawdown_pct", "profit_factor",
    ]
    print(df_summary[cols].to_string(index=False))
    print("=" * 120)

    print("\nVERDICT")
    print("-" * 120)
    for _, r in df_summary.iterrows():
        if r["sharpe_ratio"] >= 1.5 and r["max_drawdown_pct"] <= 20:
            verdict = "✅ EXCELLENT"
        elif r["sharpe_ratio"] >= 1.0 and r["max_drawdown_pct"] <= 25:
            verdict = "✅ STRONG"
        elif r["sharpe_ratio"] >= 0.5 and r["max_drawdown_pct"] <= 35:
            verdict = "🟡 ACCEPTABLE"
        elif r["sharpe_ratio"] > 0:
            verdict = "🟠 MARGINAL"
        else:
            verdict = "❌ POOR"
        print(f"  {r['ticker']:<6} {verdict:<14} "
              f"Sharpe {r['sharpe_ratio']:>5.2f} | "
              f"MaxDD {r['max_drawdown_pct']:>5.1f}% | "
              f"AnnRet {r['annual_return_pct']:>+6.1f}% | "
              f"Win {r['win_rate_pct']:>5.1f}% | "
              f"PF {r['profit_factor']:>4.2f} | "
              f"Trades {r['n_trades']:>3} "
              f"({r['profit_target_hits']} PT, {r['stop_loss_hits']} SL, "
              f"{r['expirations']} exp, {r['wing_breaks']} broke)")

    out_path = PROJECT_ROOT / "results" / f"iron_condor_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({
            "config": vars(args),
            "results": full_results,
            "summary": rows,
        }, f, indent=2, default=str)
    print(f"\n→ {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
