#!/usr/bin/env python3
"""Bulk-fetch sector ETFs + macro tickers for the sector-rotation strategy.

Downloads daily OHLCV via yfinance for:
  * 11 GICS sector ETFs:    XLK XLF XLV XLY XLP XLE XLI XLU XLB XLRE XLC
  * 4 factor ETFs:           MTUM QUAL USMV VLUE
  * 5 macro/benchmark:       SPY ^VIX ^TNX ^IRX HYG IEF

Output: data/sample/sector_rotation_<start>_<end>.parquet
        Schema: long-format frame [date, ticker, open, high, low, close, volume]

Usage:
    python scripts/fetch_sector_etf_data.py
    python scripts/fetch_sector_etf_data.py --start 2010-01-01 --end 2026-05-03
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "sample"
DATA_DIR.mkdir(parents=True, exist_ok=True)


SECTOR_ETFS: List[str] = [
    "XLK", "XLF", "XLV", "XLY", "XLP",
    "XLE", "XLI", "XLU", "XLB", "XLRE", "XLC",
]
FACTOR_ETFS: List[str] = ["MTUM", "QUAL", "USMV", "VLUE"]
MACRO_TICKERS: List[str] = ["SPY", "^VIX", "^TNX", "^IRX", "HYG", "IEF"]


def fetch_one(ticker: str, start: str, end: str) -> pd.DataFrame:
    import yfinance as yf
    try:
        df = yf.Ticker(ticker).history(
            start=start, end=end, interval="1d", auto_adjust=True,
        )
    except Exception as e:
        print(f"  {ticker} ERROR: {e}")
        return pd.DataFrame()

    if df.empty:
        print(f"  {ticker} EMPTY")
        return pd.DataFrame()

    df = df.reset_index()
    df.columns = [c.lower().replace(" ", "_") for c in df.columns]
    if "date" in df.columns:
        df = df.rename(columns={"date": "timestamp"})
    ts = pd.to_datetime(df["timestamp"])
    if hasattr(ts.dt, "tz") and ts.dt.tz is not None:
        ts = ts.dt.tz_localize(None)
    df["timestamp"] = ts
    df["ticker"] = ticker
    keep = ["timestamp", "ticker", "open", "high", "low", "close", "volume"]
    df = df[[c for c in keep if c in df.columns]]
    return df.sort_values("timestamp").reset_index(drop=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2010-01-01")
    p.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--out", default=None,
                   help="Output parquet path (default: data/sample/sector_rotation_<start>_<end>.parquet)")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    all_tickers = SECTOR_ETFS + FACTOR_ETFS + MACRO_TICKERS
    print(f"Fetching {len(all_tickers)} tickers from {args.start} to {args.end}\n")

    frames: list[pd.DataFrame] = []
    for ticker in all_tickers:
        print(f"  {ticker}...", end="", flush=True)
        df = fetch_one(ticker, args.start, args.end)
        if df.empty:
            print(" SKIP")
            continue
        first = df["timestamp"].iloc[0].strftime("%Y-%m-%d")
        last = df["timestamp"].iloc[-1].strftime("%Y-%m-%d")
        print(f" {len(df):>5} bars, {first} → {last}, ${df['close'].iloc[-1]:.2f}")
        frames.append(df)
        time.sleep(0.15)

    if not frames:
        print("No data fetched.")
        return 1

    panel = pd.concat(frames, ignore_index=True)
    panel = panel.sort_values(["ticker", "timestamp"]).reset_index(drop=True)

    out = args.out
    if out is None:
        out = DATA_DIR / f"sector_rotation_{args.start}_{args.end}.parquet"
    panel.to_parquet(out, index=False)
    size_mb = Path(out).stat().st_size / (1024 * 1024)
    n_tickers = panel["ticker"].nunique()
    print(f"\nSaved {len(panel):,} rows × {n_tickers} tickers ({size_mb:.2f} MB)")
    print(f"  → {out}")

    # Quick sanity: per-ticker bar count and date range
    print("\nPer-ticker summary:")
    print(f"  {'ticker':<8} {'bars':>6}  {'first':<12} {'last':<12}")
    for t, g in panel.groupby("ticker"):
        print(f"  {t:<8} {len(g):>6,}  {g['timestamp'].iloc[0].strftime('%Y-%m-%d'):<12} "
              f"{g['timestamp'].iloc[-1].strftime('%Y-%m-%d'):<12}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
