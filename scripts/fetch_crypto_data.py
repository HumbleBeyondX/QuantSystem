"""Fetch historical OHLCV data from Binance for crypto backtesting.

Downloads 5-minute (or any timeframe) candles via CCXT and saves to CSV.
Handles Binance API rate limits and pagination automatically.

Usage:
    python scripts/fetch_crypto_data.py
    python scripts/fetch_crypto_data.py --symbol ETH/USDT --timeframe 15m
    python scripts/fetch_crypto_data.py --start 2024-01-01 --end 2024-12-31
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def fetch_ohlcv_sync(
    symbol: str = "BTC/USDT",
    timeframe: str = "5m",
    start_date: str = "2023-01-01",
    end_date: str = "2026-03-27",
    exchange_id: str = "binance",
    output_dir: str = None,
) -> pd.DataFrame:
    """Fetch historical OHLCV data from Binance via CCXT.

    Handles pagination (Binance returns max 1000 candles per request)
    and rate limiting automatically.

    Args:
        symbol: Trading pair (e.g., "BTC/USDT").
        timeframe: Candle interval ("1m", "5m", "15m", "1h", "4h", "1d").
        start_date: Start date string "YYYY-MM-DD".
        end_date: End date string "YYYY-MM-DD".
        exchange_id: CCXT exchange ID (default: "binance").
        output_dir: Directory to save CSV. If None, saves to data/crypto/.

    Returns:
        DataFrame with columns: timestamp, open, high, low, close, volume.
    """
    import ccxt

    exchange_class = getattr(ccxt, exchange_id)
    exchange = exchange_class({"enableRateLimit": True})

    # Parse dates to millisecond timestamps
    start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp() * 1000)
    end_ts = int(datetime.strptime(end_date, "%Y-%m-%d").timestamp() * 1000)

    # Timeframe to milliseconds
    tf_ms = {
        "1m": 60_000, "3m": 180_000, "5m": 300_000,
        "15m": 900_000, "30m": 1_800_000,
        "1h": 3_600_000, "4h": 14_400_000,
        "1d": 86_400_000,
    }
    interval_ms = tf_ms.get(timeframe, 300_000)
    batch_size = 1000  # Binance max per request
    batch_ms = interval_ms * batch_size

    all_candles = []
    current_ts = start_ts
    total_expected = (end_ts - start_ts) / interval_ms

    print(f"\n{'='*60}")
    print(f"  Fetching {symbol} {timeframe} from {exchange_id}")
    print(f"  Range: {start_date} to {end_date}")
    print(f"  Expected candles: ~{int(total_expected):,}")
    print(f"{'='*60}\n")

    batch_num = 0
    while current_ts < end_ts:
        try:
            candles = exchange.fetch_ohlcv(
                symbol, timeframe,
                since=current_ts,
                limit=batch_size,
            )

            if not candles:
                break

            all_candles.extend(candles)
            batch_num += 1

            # Move to next batch
            last_ts = candles[-1][0]
            current_ts = last_ts + interval_ms

            # Progress
            progress = min(100, (current_ts - start_ts) / (end_ts - start_ts) * 100)
            print(
                f"  Batch {batch_num}: {len(candles)} candles | "
                f"Total: {len(all_candles):,} | "
                f"Progress: {progress:.1f}% | "
                f"Latest: {datetime.fromtimestamp(last_ts / 1000).strftime('%Y-%m-%d %H:%M')}"
            )

            # Rate limiting (Binance allows ~1200 req/min)
            time.sleep(0.5)

        except Exception as e:
            print(f"  Error at batch {batch_num}: {e}")
            time.sleep(5)  # Back off on error
            continue

    if not all_candles:
        print("  No data fetched!")
        return pd.DataFrame()

    # Convert to DataFrame
    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")

    # Remove duplicates and sort
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

    # Filter to exact date range
    start_dt = pd.Timestamp(start_date)
    end_dt = pd.Timestamp(end_date) + pd.Timedelta(days=1)
    df = df[(df["timestamp"] >= start_dt) & (df["timestamp"] < end_dt)]

    print(f"\n  Final dataset: {len(df):,} candles")
    print(f"  Date range: {df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]}")
    print(f"  Price range: ${df['close'].min():,.2f} - ${df['close'].max():,.2f}")

    # Save to CSV
    if output_dir is None:
        output_dir = ROOT / "data" / "crypto"
    else:
        output_dir = Path(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)
    safe_symbol = symbol.replace("/", "_")
    filename = f"{safe_symbol}_{timeframe}_{start_date}_to_{end_date}.csv"
    filepath = output_dir / filename

    df.to_csv(filepath, index=False)
    print(f"  Saved to: {filepath}")
    print(f"  File size: {filepath.stat().st_size / 1024 / 1024:.1f} MB")

    return df


def main():
    parser = argparse.ArgumentParser(description="Fetch crypto OHLCV data from Binance")
    parser.add_argument("--symbol", default="BTC/USDT", help="Trading pair")
    parser.add_argument("--timeframe", default="5m", help="Candle interval (1m, 5m, 15m, 1h)")
    parser.add_argument("--start", default="2023-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2026-03-27", help="End date YYYY-MM-DD")
    parser.add_argument("--exchange", default="binanceus", help="Exchange ID (binanceus, okx, kraken, coinbase)")
    parser.add_argument("--output", default=None, help="Output directory")
    args = parser.parse_args()

    fetch_ohlcv_sync(
        symbol=args.symbol,
        timeframe=args.timeframe,
        start_date=args.start,
        end_date=args.end,
        exchange_id=args.exchange,
        output_dir=args.output,
    )


if __name__ == "__main__":
    main()
