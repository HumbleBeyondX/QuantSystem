#!/usr/bin/env python3
"""Fetch historical ETH 1H OHLCV data from OKX (with Binance cross-check).

Downloads ETH/USDT 1H candles for the regime-switching strategy backtest.
Saves to data/sample/ETH_USDT_1h_<start>_to_<end>.csv.

Usage:
    python scripts/fetch_eth_1h_data.py
    python scripts/fetch_eth_1h_data.py --start 2024-01-01 --end 2026-04-16
    python scripts/fetch_eth_1h_data.py --exchange binance   # cross-check

Notes:
    * OKX is the primary source (matches the live trading venue).
    * A cross-check run with --exchange binance produces a second file.
      Compare them to detect exchange-specific anomalies (spikes, gaps).
    * Rate-limited at 0.3s between requests — well under OKX's 10 req/s.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "sample"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def fetch_ohlcv(
    exchange: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """Fetch OHLCV candles in batches (1000 per request)."""
    since = exchange.parse8601(f"{start_date}T00:00:00Z")
    end_ts = exchange.parse8601(f"{end_date}T23:59:59Z")

    all_candles: list[list] = []
    batch_size = 1000
    batch_count = 0

    print(f"  Fetching {symbol} {timeframe} from {start_date} to {end_date}...")

    while since < end_ts:
        try:
            candles = exchange.fetch_ohlcv(
                symbol, timeframe, since=since, limit=batch_size
            )
            if not candles:
                print("  → empty batch, stopping")
                break

            all_candles.extend(candles)
            batch_count += 1

            last_ts = candles[-1][0]
            last_dt = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc)
            print(
                f"  [{batch_count}] {len(candles):>4} candles, "
                f"up to {last_dt.strftime('%Y-%m-%d %H:%M')} UTC "
                f"(total: {len(all_candles)})"
            )

            # Advance past the last candle
            since = last_ts + 1

            # If the last candle is already past end_ts, we're done
            if last_ts >= end_ts:
                break

            # Rate limit: well under OKX's 10 req/s and Binance's 20 req/s
            time.sleep(0.3)

        except ccxt.RateLimitExceeded:
            print("  → rate limit; sleeping 5s")
            time.sleep(5)
        except Exception as e:
            print(f"  ERROR: {e}")
            time.sleep(2)
            # Try once more before giving up
            try:
                candles = exchange.fetch_ohlcv(
                    symbol, timeframe, since=since, limit=batch_size
                )
                all_candles.extend(candles)
                since = candles[-1][0] + 1
            except Exception as e2:
                print(f"  fatal: {e2}")
                break

    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(
        all_candles,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)

    # Trim to requested range (inclusive start, exclusive end day+1)
    start_dt = pd.Timestamp(start_date, tz="UTC")
    end_dt = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)
    df = df[(df["timestamp"] >= start_dt) & (df["timestamp"] < end_dt)].reset_index(drop=True)
    return df


def gap_report(df: pd.DataFrame, timeframe_seconds: int) -> None:
    """Print a summary of bar-to-bar gaps. Gaps > timeframe → missing data."""
    if len(df) < 2:
        return
    diffs = df["timestamp"].diff().dt.total_seconds().dropna()
    expected = timeframe_seconds
    gaps = diffs[diffs > expected * 1.5]
    if len(gaps) == 0:
        print(f"  Gaps: none (no missing bars)")
        return
    print(f"  Gaps: {len(gaps)} detected (largest: {gaps.max() / 3600:.1f}h)")
    worst = gaps.nlargest(5)
    for idx, g in worst.items():
        ts = df.loc[idx, "timestamp"]
        print(f"    - {ts} → gap of {g / 3600:.1f}h")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fetch ETH 1H OHLCV history")
    p.add_argument("--symbol", default="ETH/USDT")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--start", default="2024-01-01")
    p.add_argument("--end", default=datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    p.add_argument("--exchange", default="okx", choices=["okx", "binance", "bybit"])
    return p.parse_args()


def main() -> int:
    args = parse_args()

    ex_class = getattr(ccxt, args.exchange)
    exchange = ex_class({
        "enableRateLimit": True,
        "options": {"defaultType": "spot"},
    })

    print(f"Exchange: {exchange.id}")
    print(f"Symbol:   {args.symbol}")
    print(f"Timeframe: {args.timeframe}")
    print(f"Range:    {args.start} → {args.end}\n")

    df = fetch_ohlcv(
        exchange,
        args.symbol,
        args.timeframe,
        args.start,
        args.end,
    )

    if df.empty:
        print("No data fetched.")
        return 1

    print(f"\nFetched {len(df):,} bars")
    print(f"  First: {df['timestamp'].iloc[0]}")
    print(f"  Last:  {df['timestamp'].iloc[-1]}")

    tf_seconds = {
        "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
        "1h": 3600, "4h": 14400, "1d": 86400,
    }.get(args.timeframe, 3600)
    gap_report(df, tf_seconds)

    # Output filename: ETH_USDT_1h_<start>_to_<end>_<exchange>.csv
    safe_symbol = args.symbol.replace("/", "_")
    fname = f"{safe_symbol}_{args.timeframe}_{args.start}_to_{args.end}_{exchange.id}.csv"
    out = DATA_DIR / fname
    df.to_csv(out, index=False)
    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"\nSaved: {out}  ({size_mb:.2f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
