#!/usr/bin/env python3
"""Fetch historical 4H OHLCV data for BTC/USDT and ETH/USDT from Binance.

Downloads data for macro cycle strategy backtesting.
Saves to data/sample/ for integration with the dashboard backtest API.

Usage:
    python scripts/fetch_crypto_4h_data.py
    python scripts/fetch_crypto_4h_data.py --symbols BTC/USDT ETH/USDT
    python scripts/fetch_crypto_4h_data.py --start 2020-01-01 --end 2026-03-27
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

import ccxt
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "sample"


def fetch_ohlcv(
    exchange: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """Fetch OHLCV data in batches (Binance limit: 1000 candles per request)."""
    since = exchange.parse8601(f"{start_date}T00:00:00Z")
    end_ts = exchange.parse8601(f"{end_date}T23:59:59Z")

    all_candles = []
    batch_size = 1000

    print(f"  Fetching {symbol} {timeframe} from {start_date} to {end_date}...")

    while since < end_ts:
        try:
            candles = exchange.fetch_ohlcv(
                symbol, timeframe, since=since, limit=batch_size
            )
            if not candles:
                break

            all_candles.extend(candles)
            since = candles[-1][0] + 1  # Move past last candle timestamp
            time.sleep(0.3)  # Rate limit

            if len(all_candles) % 5000 == 0:
                print(f"    ... {len(all_candles)} candles fetched so far")

        except ccxt.RateLimitExceeded:
            print("    Rate limited, waiting 10s...")
            time.sleep(10)
        except Exception as e:
            print(f"    Error: {e}, retrying in 5s...")
            time.sleep(5)

    if not all_candles:
        print(f"  No data fetched for {symbol}")
        return pd.DataFrame()

    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

    # Filter to date range
    df = df[df["timestamp"] <= pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)]

    return df


def main():
    parser = argparse.ArgumentParser(description="Fetch crypto 4H OHLCV data")
    parser.add_argument("--symbols", nargs="+", default=["BTC/USDT", "ETH/USDT"])
    parser.add_argument("--timeframe", default="4h")
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2026-03-27")
    parser.add_argument("--exchange", default="binance",
                        choices=["binance", "binanceus", "kucoin", "kraken", "okx"])
    args = parser.parse_args()

    # Initialize exchange
    exchange_class = getattr(ccxt, args.exchange)
    exchange = exchange_class({
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    for symbol in args.symbols:
        print(f"\n{'='*60}")
        print(f"Fetching {symbol} {args.timeframe} data")
        print(f"{'='*60}")

        df = fetch_ohlcv(exchange, symbol, args.timeframe, args.start, args.end)

        if df.empty:
            print(f"  FAILED: no data for {symbol}")
            continue

        # Save with standardized filename for dashboard auto-detection
        safe_symbol = symbol.replace("/", "_")
        filename = f"{safe_symbol}_{args.timeframe}.csv"
        filepath = DATA_DIR / filename

        df.to_csv(filepath, index=False)

        print(f"\n  Saved: {filepath}")
        print(f"  Rows: {len(df):,}")
        print(f"  Date range: {df['timestamp'].iloc[0]} → {df['timestamp'].iloc[-1]}")
        print(f"  Size: {filepath.stat().st_size / (1024*1024):.1f} MB")

        # Also save with specific date range name for reference
        date_filename = f"{safe_symbol}_{args.timeframe}_{args.start}_to_{args.end}.csv"
        date_filepath = DATA_DIR / date_filename
        df.to_csv(date_filepath, index=False)
        print(f"  Also saved as: {date_filepath.name}")

    print(f"\n{'='*60}")
    print("Done! Data ready for backtesting.")
    print(f"Data directory: {DATA_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
