#!/usr/bin/env python3
"""Run the wheel-strategy backtest on one or more tickers.

Pulls daily OHLCV from yfinance, runs wheel_backtest engine, prints metrics.

Usage:
    python scripts/backtest_wheel.py --tickers RIOT TQQQ INTC SOXL
    python scripts/backtest_wheel.py --tickers PLTR --capital 15000 --delta 0.20
    python scripts/backtest_wheel.py --tickers RIOT --start 2024-01-01 --end 2026-04-30
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import List

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from research.wheel_backtest import run_wheel_backtest  # noqa: E402


def fetch_daily(ticker: str, start: str, end: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.Ticker(ticker).history(start=start, end=end, interval="1d", auto_adjust=True)
    if df.empty:
        return pd.DataFrame()
    df = df.reset_index()
    df.columns = [c.lower().replace(" ", "_") for c in df.columns]
    # yfinance returns "date" after reset_index; normalize to "timestamp"
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
    p.add_argument("--delta", type=float, default=0.25)
    p.add_argument("--dte", type=int, default=30)
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default=datetime.utcnow().strftime("%Y-%m-%d"))
    p.add_argument("--vrp", type=float, default=1.15, help="IV/RV multiplier")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    print(f"Backtest period: {args.start} → {args.end}")
    print(f"Capital: ${args.capital:,.0f} | Delta: {args.delta} | DTE: {args.dte}\n")

    rows = []
    full_results = {}
    for ticker in args.tickers:
        print(f"  Fetching {ticker}...", end="", flush=True)
        df = fetch_daily(ticker, args.start, args.end)
        if df.empty or len(df) < 60:
            print(f" SKIP ({len(df)} bars)")
            continue
        print(f" {len(df)} bars, ${df['close'].iloc[0]:.2f} → ${df['close'].iloc[-1]:.2f}")

        result = run_wheel_backtest(
            ticker, df,
            initial_capital=args.capital,
            target_delta=args.delta,
            target_dte=args.dte,
            vrp_multiplier=args.vrp,
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

    # ── Comparative table ──
    df_summary = pd.DataFrame(rows)
    print("\n" + "=" * 110)
    print("WHEEL BACKTEST RESULTS (sorted by Sharpe)")
    print("=" * 110)
    df_summary = df_summary.sort_values("sharpe_ratio", ascending=False)
    cols = [
        "ticker", "n_trades", "expired_otm", "assignments", "callaways",
        "total_premium", "final_value", "total_return_pct",
        "annual_return_pct", "annual_volatility", "sharpe_ratio",
        "max_drawdown_pct", "win_rate_pct",
    ]
    print(df_summary[cols].to_string(index=False))
    print("=" * 110)

    # ── Verdict per ticker ──
    print("\nVERDICT")
    print("-" * 110)
    for _, r in df_summary.iterrows():
        verdict_pieces = []
        if r["sharpe_ratio"] > 1.0 and r["max_drawdown_pct"] < 25:
            verdict = "✅ STRONG"
        elif r["sharpe_ratio"] > 0.5 and r["max_drawdown_pct"] < 40:
            verdict = "🟡 ACCEPTABLE"
        elif r["sharpe_ratio"] > 0:
            verdict = "🟠 MARGINAL"
        else:
            verdict = "❌ POOR"
        print(f"  {r['ticker']:<6} {verdict:<14} "
              f"Sharpe {r['sharpe_ratio']:>5.2f} | "
              f"MaxDD {r['max_drawdown_pct']:>5.1f}% | "
              f"AnnRet {r['annual_return_pct']:>6.1f}% | "
              f"Trades {r['n_trades']:>3} ({r['expired_otm']} OTM, "
              f"{r['assignments']} asg, {r['callaways']} call)")

    # Save
    out_path = PROJECT_ROOT / "results" / f"wheel_backtest_{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
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
