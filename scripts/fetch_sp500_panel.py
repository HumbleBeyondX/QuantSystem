#!/usr/bin/env python3
"""Fetch S&P 500 constituents' daily OHLCV → long-format parquet panel.

Pipeline:
  1. Pull current SP500 ticker list from Wikipedia (cached fallback)
  2. Bulk download via yfinance (batched + sleep for rate limit)
  3. Validate per-ticker bar counts; drop tickers with <100 bars
  4. Save as long-format parquet (timestamp, ticker, OHLCV)

Survivorship bias note: this uses the CURRENT SP500 list, so the
backtest universe biases toward survivors. For research-grade
backtests one would want point-in-time membership history. We document
this caveat and accept it for now (significantly higher computational
cost otherwise).

Usage:
    python scripts/fetch_sp500_panel.py
    python scripts/fetch_sp500_panel.py --start 2015-01-01 --batch-size 50
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


SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
TICKER_CACHE = DATA_DIR / "sp500_tickers.txt"


def get_sp500_tickers(use_cache: bool = True) -> List[str]:
    """Get current SP500 constituents. Uses Wikipedia + local cache fallback."""
    if use_cache and TICKER_CACHE.exists():
        cached = [t.strip() for t in TICKER_CACHE.read_text().splitlines() if t.strip()]
        if len(cached) > 400:
            print(f"  Using cached ticker list ({len(cached)} tickers)")
            return cached

    print(f"  Fetching SP500 list from Wikipedia...")
    try:
        tables = pd.read_html(SP500_WIKI_URL)
        df = tables[0]
        # Symbol column may have names like 'BRK.B' which yfinance wants 'BRK-B'
        tickers = df["Symbol"].astype(str).str.replace(".", "-", regex=False).tolist()
        TICKER_CACHE.write_text("\n".join(tickers))
        print(f"  Cached {len(tickers)} tickers → {TICKER_CACHE}")
        return tickers
    except Exception as e:
        print(f"  WARN: Wikipedia fetch failed: {e}")
        # Fallback: hardcoded set of liquid tickers (mega-caps + sectors)
        fallback = [
            # Tech
            "AAPL", "MSFT", "GOOGL", "GOOG", "META", "NVDA", "AMD", "ORCL",
            "CRM", "ADBE", "AVGO", "INTC", "CSCO", "IBM", "NOW", "PYPL",
            "QCOM", "TXN", "MU", "AMAT", "LRCX", "KLAC", "PANW", "MDB",
            # Consumer
            "AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "LOW", "TGT",
            "COST", "WMT", "DIS", "NFLX", "BKNG", "CMG", "PG", "KO",
            "PEP", "MDLZ", "PM", "MO", "KHC", "HSY", "GIS",
            # Financials
            "JPM", "BAC", "WFC", "GS", "MS", "C", "AXP", "USB", "PNC",
            "TFC", "BLK", "SCHW", "COF", "MET", "PRU", "AIG", "ALL",
            "TRV", "PGR", "AON", "MMC", "BX", "KKR", "CME",
            # Healthcare
            "UNH", "JNJ", "LLY", "PFE", "ABBV", "MRK", "ABT", "TMO",
            "DHR", "BMY", "AMGN", "GILD", "CVS", "ISRG", "VRTX", "REGN",
            "ZTS", "MDT", "ELV", "BDX", "SYK", "BSX",
            # Industrials
            "HON", "RTX", "UPS", "BA", "CAT", "GE", "MMM", "DE", "UNP",
            "FDX", "ETN", "EMR", "ITW", "WM", "CSX", "NSC", "LMT",
            "NOC", "GD", "PCAR", "ROP", "PH",
            # Energy
            "XOM", "CVX", "COP", "EOG", "SLB", "OXY", "PSX", "VLO",
            "MPC", "PXD", "HES",
            # Other
            "BRK-B", "V", "MA", "F", "GM", "T", "VZ", "TMUS", "CMCSA",
            "LIN", "APD", "SHW", "ECL", "FCX", "NEM", "NUE", "VMC",
            "PLD", "AMT", "CCI", "EQIX", "WELL", "PSA", "NEE", "DUK",
            "SO", "AEP", "EXC", "D", "SRE",
        ]
        TICKER_CACHE.write_text("\n".join(fallback))
        print(f"  Using fallback list of {len(fallback)} mega-cap tickers")
        return fallback


def fetch_one(ticker: str, start: str, end: str) -> pd.DataFrame:
    import yfinance as yf
    try:
        df = yf.Ticker(ticker).history(start=start, end=end, interval="1d", auto_adjust=True)
    except Exception:
        return pd.DataFrame()
    if df.empty:
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
    return df[[c for c in keep if c in df.columns]].sort_values("timestamp").reset_index(drop=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--start", default="2010-01-01")
    p.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--batch-size", type=int, default=20,
                   help="Tickers per batch (with sleep between)")
    p.add_argument("--sleep", type=float, default=0.15,
                   help="Sleep between fetches (sec)")
    p.add_argument("--out", default=None)
    p.add_argument("--max-tickers", type=int, default=None,
                   help="Limit ticker count (debug)")
    p.add_argument("--no-cache", action="store_true",
                   help="Refetch ticker list from Wikipedia")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    print(f"S&P 500 panel fetch: {args.start} → {args.end}\n")

    tickers = get_sp500_tickers(use_cache=not args.no_cache)
    if args.max_tickers:
        tickers = tickers[: args.max_tickers]
    print(f"\nFetching {len(tickers)} tickers...\n")

    frames: list[pd.DataFrame] = []
    failed: list[str] = []
    t_start = time.time()
    for i, ticker in enumerate(tickers, 1):
        df = fetch_one(ticker, args.start, args.end)
        if df.empty or len(df) < 100:
            failed.append(ticker)
            print(f"  [{i:>3}/{len(tickers)}] {ticker:<8} SKIP")
        else:
            frames.append(df)
            if i % 20 == 0 or i == len(tickers):
                elapsed = time.time() - t_start
                rate = i / elapsed if elapsed > 0 else 0
                eta = (len(tickers) - i) / rate if rate > 0 else 0
                print(f"  [{i:>3}/{len(tickers)}] {ticker:<8} {len(df):>5} bars  "
                      f"({rate:.1f}/s, ETA {eta/60:.1f}min)")
        time.sleep(args.sleep)

    if not frames:
        print("\nNo data fetched.")
        return 1

    panel = pd.concat(frames, ignore_index=True)
    panel = panel.sort_values(["ticker", "timestamp"]).reset_index(drop=True)

    out = args.out
    if out is None:
        out = DATA_DIR / f"sp500_{args.start}_{args.end}.parquet"
    panel.to_parquet(out, index=False)
    size_mb = Path(out).stat().st_size / (1024 * 1024)
    n = panel["ticker"].nunique()

    print(f"\nSaved {len(panel):,} rows × {n} tickers ({size_mb:.1f} MB)")
    print(f"  → {out}")
    if failed:
        print(f"\nFailed/skipped ({len(failed)}): {', '.join(failed[:20])}"
              + ("..." if len(failed) > 20 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
