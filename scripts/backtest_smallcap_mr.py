#!/usr/bin/env python3
"""Run small-cap mean-reversion backtest across an IWM + small-cap basket.

Default basket = IWM (Russell 2000 ETF) + 10 individual high-vol small caps.

Usage:
    python scripts/backtest_smallcap_mr.py
    python scripts/backtest_smallcap_mr.py --tickers IWM AAL CCL F
    python scripts/backtest_smallcap_mr.py --drop 4 --pt 2.5 --sl 4
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

from research.smallcap_mean_reversion import run_smallcap_mr_backtest  # noqa: E402


# Default basket: IWM + a curated set of small/mid cap names with
# enough vol to actually trigger the entry filter regularly.
DEFAULT_BASKET = [
    "IWM",      # Russell 2000 ETF — the index baseline
    "F",        # Ford
    "BAC",      # Bank of America (mid)
    "AAL",      # American Airlines
    "CCL",      # Carnival Cruise
    "RIVN",     # Rivian
    "PLUG",     # Plug Power
    "FCEL",     # FuelCell Energy
    "X",        # US Steel
    "CLF",      # Cleveland-Cliffs
    "SOFI",     # SoFi
    "NIO",      # NIO (Chinese small-cap EV)
]


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
    p.add_argument("--tickers", nargs="+", default=DEFAULT_BASKET)
    p.add_argument("--capital", type=float, default=10_000)
    p.add_argument("--drop", type=float, default=3.0, help="3-day cum return drop %% to enter")
    p.add_argument("--rsi-os", type=float, default=15.0, help="RSI(2) oversold threshold")
    p.add_argument("--rsi-ob", type=float, default=70.0, help="RSI(2) overbought threshold")
    p.add_argument("--pt", type=float, default=2.0, help="Profit target %%")
    p.add_argument("--sl", type=float, default=3.0, help="Stop loss %%")
    p.add_argument("--max-hold", type=int, default=5)
    p.add_argument("--sma", type=int, default=200, help="SMA filter period")
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--no-sma", action="store_true",
                   help="Disable the long-trend SMA filter (allow trades in downtrends)")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    print(f"Small-Cap Mean Reversion backtest")
    print(f"  Period:           {args.start} → {args.end}")
    print(f"  Capital:          ${args.capital:,.0f}")
    print(f"  Drop threshold:   3-day cum return < -{args.drop}%")
    print(f"  RSI(2) thresholds: oversold {args.rsi_os}, overbought {args.rsi_ob}")
    print(f"  Profit target:    +{args.pt}%")
    print(f"  Stop loss:        -{args.sl}%")
    print(f"  Max hold:         {args.max_hold} days")
    print(f"  Trend filter:     {'OFF' if args.no_sma else f'close > SMA{args.sma}'}\n")

    rows = []
    full_results = {}
    for ticker in args.tickers:
        print(f"  {ticker}...", end="", flush=True)
        df = fetch_daily(ticker, args.start, args.end)
        if df.empty or len(df) < 220:
            print(f" SKIP ({len(df)} bars)")
            continue
        print(f" {len(df)} bars, ${df['close'].iloc[0]:.2f} → ${df['close'].iloc[-1]:.2f}")

        result = run_smallcap_mr_backtest(
            ticker, df,
            initial_capital=args.capital,
            drop_threshold_pct=args.drop,
            rsi2_oversold=args.rsi_os,
            rsi2_overbought=args.rsi_ob,
            profit_target_pct=args.pt,
            stop_loss_pct=args.sl,
            max_hold_days=args.max_hold,
            sma_filter=None if args.no_sma else args.sma,
        )
        m = result.metrics()
        rows.append(m)
        full_results[ticker] = {
            "metrics": m,
            "trades": [asdict(t) for t in result.trades],
        }

    if not rows:
        print("No backtests completed.")
        return 1

    df_summary = pd.DataFrame(rows)
    print("\n" + "=" * 130)
    print("SMALL-CAP MEAN REVERSION RESULTS (sorted by Sharpe)")
    print("=" * 130)
    df_summary = df_summary.sort_values("sharpe_ratio", ascending=False)
    cols = [
        "ticker", "n_trades", "win_rate_pct", "profit_factor",
        "avg_days_held", "time_in_market_pct",
        "final_value", "total_return_pct", "annual_return_pct",
        "sharpe_ratio", "max_drawdown_pct",
        "avg_win_pct", "avg_loss_pct",
    ]
    print(df_summary[cols].to_string(index=False))
    print("=" * 130)

    print("\nVERDICT")
    print("-" * 130)
    for _, r in df_summary.iterrows():
        if r["sharpe_ratio"] >= 1.5 and r["max_drawdown_pct"] <= 15 and r["win_rate_pct"] >= 65:
            verdict = "✅ EXCELLENT"
        elif r["sharpe_ratio"] >= 1.0 and r["max_drawdown_pct"] <= 25 and r["win_rate_pct"] >= 60:
            verdict = "✅ STRONG"
        elif r["sharpe_ratio"] >= 0.5:
            verdict = "🟡 OK"
        elif r["sharpe_ratio"] > 0:
            verdict = "🟠 WEAK"
        else:
            verdict = "❌ POOR"
        print(f"  {r['ticker']:<6} {verdict:<14} "
              f"Sharpe {r['sharpe_ratio']:>5.2f} | "
              f"MaxDD {r['max_drawdown_pct']:>5.1f}% | "
              f"AnnRet {r['annual_return_pct']:>+6.1f}% | "
              f"Win {r['win_rate_pct']:>5.1f}% | "
              f"PF {r['profit_factor']:>4.2f} | "
              f"Trades {r['n_trades']:>3} | "
              f"AvgDays {r['avg_days_held']:>3.1f}")

    out_path = PROJECT_ROOT / "results" / f"smallcap_mr_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
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
