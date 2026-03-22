#!/usr/bin/env python3
"""Fetch real market data from exchanges via ccxt for backtesting.

Supports OKX, Binance, Bybit. Downloads OHLCV klines and recent trades.

Usage examples:
    # Fetch 7 days of 1m BTC/USDT klines from OKX
    python scripts/fetch_market_data.py --exchange okx --symbol BTC/USDT --timeframe 1m --days 7

    # Fetch 30 days of 1m data for multiple symbols
    python scripts/fetch_market_data.py --exchange okx --symbol BTC/USDT ETH/USDT --timeframe 1m --days 30

    # Fetch recent trades (tick-level proxy)
    python scripts/fetch_market_data.py --exchange okx --symbol BTC/USDT --mode trades --limit 10000
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("fetch_market_data")

SAMPLE_DIR = PROJECT_ROOT / "data" / "sample"


# ---------------------------------------------------------------------------
# Exchange factory
# ---------------------------------------------------------------------------

def create_exchange(name: str, config: Optional[Dict] = None):
    """Create a ccxt exchange instance."""
    import ccxt

    exchange_classes = {
        "okx": ccxt.okx,
        "binance": ccxt.binance,
        "bybit": ccxt.bybit,
    }

    if name.lower() not in exchange_classes:
        raise ValueError(f"Unsupported exchange: {name}. Supported: {list(exchange_classes.keys())}")

    default_config = {"enableRateLimit": True}
    if config:
        default_config.update(config)

    exchange = exchange_classes[name.lower()](default_config)
    logger.info("Created exchange: %s", exchange.id)
    return exchange


# ---------------------------------------------------------------------------
# OHLCV fetcher (klines)
# ---------------------------------------------------------------------------

def fetch_ohlcv(
    exchange,
    symbol: str,
    timeframe: str = "1m",
    since_ms: int = 0,
    end_ms: int = 0,
    limit_per_request: int = 300,
) -> pd.DataFrame:
    """Fetch OHLCV klines with pagination.

    Args:
        exchange: ccxt exchange instance
        symbol: Trading pair (e.g. "BTC/USDT")
        timeframe: Candle interval ("1m", "5m", "15m", "1h", "1d")
        since_ms: Start timestamp in milliseconds
        end_ms: End timestamp in milliseconds
        limit_per_request: Max candles per request (exchange-dependent)

    Returns:
        DataFrame with columns: timestamp, open, high, low, close, volume
    """
    all_candles: List = []
    current_since = since_ms
    request_count = 0

    tf_ms = _timeframe_to_ms(timeframe)

    while current_since < end_ms:
        try:
            candles = exchange.fetch_ohlcv(
                symbol,
                timeframe=timeframe,
                since=current_since,
                limit=limit_per_request,
            )
        except Exception as e:
            logger.warning("Request failed (attempt will retry): %s", e)
            time.sleep(2)
            continue

        if not candles:
            break

        all_candles.extend(candles)
        request_count += 1

        # Advance past last candle
        last_ts = candles[-1][0]
        current_since = last_ts + tf_ms

        if request_count % 10 == 0:
            logger.info(
                "  Fetched %d candles so far (up to %s)",
                len(all_candles),
                datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            )

        # Rate limiting
        time.sleep(exchange.rateLimit / 1000 * 1.1)

    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)

    # Filter to requested range
    end_dt = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    df = df[df["timestamp"] <= end_dt]

    logger.info("Fetched %d %s candles for %s (%d requests)", len(df), timeframe, symbol, request_count)
    return df


# ---------------------------------------------------------------------------
# Trades fetcher (tick-level proxy)
# ---------------------------------------------------------------------------

def fetch_trades(
    exchange,
    symbol: str,
    since_ms: int = 0,
    end_ms: int = 0,
    max_trades: int = 50000,
) -> pd.DataFrame:
    """Fetch recent trades as tick-level proxy data.

    Args:
        exchange: ccxt exchange instance
        symbol: Trading pair
        since_ms: Start timestamp (ms)
        end_ms: End timestamp (ms)
        max_trades: Maximum total trades to fetch

    Returns:
        DataFrame with columns: timestamp, price, amount, side, cost
    """
    all_trades: List = []
    current_since = since_ms
    request_count = 0

    while len(all_trades) < max_trades:
        try:
            trades = exchange.fetch_trades(
                symbol,
                since=current_since,
                limit=1000,
            )
        except Exception as e:
            logger.warning("Trades request failed: %s", e)
            time.sleep(2)
            continue

        if not trades:
            break

        # Filter to time range
        filtered = [t for t in trades if t["timestamp"] <= end_ms]
        all_trades.extend(filtered)
        request_count += 1

        if filtered[-1]["timestamp"] >= end_ms:
            break

        current_since = trades[-1]["timestamp"] + 1

        if request_count % 10 == 0:
            logger.info("  Fetched %d trades so far", len(all_trades))

        time.sleep(exchange.rateLimit / 1000 * 1.1)

    if not all_trades:
        return pd.DataFrame()

    df = pd.DataFrame([{
        "timestamp": t["timestamp"],
        "price": t["price"],
        "amount": t["amount"],
        "side": t["side"],
        "cost": t["cost"],
    } for t in all_trades])

    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates().sort_values("timestamp").reset_index(drop=True)

    logger.info("Fetched %d trades for %s (%d requests)", len(df), symbol, request_count)
    return df


def trades_to_tick_bars(trades_df: pd.DataFrame, n_trades: int = 50) -> pd.DataFrame:
    """Aggregate trades into tick bars (every N trades).

    Creates OHLCV bars where each bar represents exactly N trades.
    Better for scalping backtests than time-based bars.
    """
    if trades_df.empty:
        return pd.DataFrame()

    bars = []
    for i in range(0, len(trades_df) - n_trades + 1, n_trades):
        chunk = trades_df.iloc[i:i + n_trades]
        bars.append({
            "timestamp": chunk["timestamp"].iloc[0],
            "open": chunk["price"].iloc[0],
            "high": chunk["price"].max(),
            "low": chunk["price"].min(),
            "close": chunk["price"].iloc[-1],
            "volume": chunk["amount"].sum(),
            "n_trades": len(chunk),
            "buy_volume": chunk.loc[chunk["side"] == "buy", "amount"].sum(),
            "sell_volume": chunk.loc[chunk["side"] == "sell", "amount"].sum(),
            "vwap": (chunk["price"] * chunk["amount"]).sum() / chunk["amount"].sum()
                if chunk["amount"].sum() > 0 else chunk["price"].mean(),
        })

    return pd.DataFrame(bars)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timeframe_to_ms(tf: str) -> int:
    """Convert timeframe string to milliseconds."""
    mapping = {
        "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
        "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000,
        "1d": 86_400_000, "1w": 604_800_000,
    }
    return mapping.get(tf, 60_000)


def save_data(df: pd.DataFrame, symbol: str, suffix: str) -> Path:
    """Save DataFrame to CSV."""
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = symbol.replace("/", "_")
    path = SAMPLE_DIR / f"{safe_name}_{suffix}.csv"
    df.to_csv(path, index=False)
    logger.info("Saved %d rows to %s", len(df), path)
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch market data from exchanges")
    parser.add_argument("--exchange", default="okx", choices=["okx", "binance", "bybit"])
    parser.add_argument("--symbol", nargs="+", default=["BTC/USDT"], help="Trading pair(s)")
    parser.add_argument("--timeframe", default="1m", help="OHLCV timeframe (default: 1m)")
    parser.add_argument("--days", type=int, default=7, help="Days of history (default: 7)")
    parser.add_argument("--start", type=str, default=None, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default=None, help="End date (YYYY-MM-DD)")
    parser.add_argument("--mode", default="ohlcv", choices=["ohlcv", "trades", "both"])
    parser.add_argument("--max-trades", type=int, default=50000, help="Max trades to fetch")
    parser.add_argument("--output-dir", default=str(SAMPLE_DIR))
    return parser.parse_args()


def main():
    args = parse_args()
    exchange = create_exchange(args.exchange)

    now = datetime.now(timezone.utc)

    # Support absolute date ranges via --start / --end
    if args.start:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        since_ms = int(start_dt.timestamp() * 1000)
    else:
        since_ms = int((now - timedelta(days=args.days)).timestamp() * 1000)

    if args.end:
        end_dt = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_ms = int(end_dt.timestamp() * 1000)
    else:
        end_ms = int(now.timestamp() * 1000)

    for symbol in args.symbol:
        logger.info("=" * 60)
        logger.info("Fetching %s from %s", symbol, args.exchange.upper())
        logger.info("=" * 60)

        if args.mode in ("ohlcv", "both"):
            df = fetch_ohlcv(exchange, symbol, args.timeframe, since_ms, end_ms)
            if not df.empty:
                save_data(df, symbol, args.timeframe)

                # Print summary
                logger.info("--- OHLCV Summary ---")
                logger.info("  Rows: %d", len(df))
                logger.info("  Time: %s to %s", df["timestamp"].iloc[0], df["timestamp"].iloc[-1])
                logger.info("  Price: $%.2f - $%.2f", df["low"].min(), df["high"].max())
                logger.info("  Vol: %.2f", df["volume"].sum())

        if args.mode in ("trades", "both"):
            trades = fetch_trades(exchange, symbol, since_ms, end_ms, args.max_trades)
            if not trades.empty:
                save_data(trades, symbol, "trades")

                # Also generate tick bars
                tick_bars = trades_to_tick_bars(trades, n_trades=50)
                if not tick_bars.empty:
                    save_data(tick_bars, symbol, "tick_bars")

                logger.info("--- Trades Summary ---")
                logger.info("  Trades: %d", len(trades))
                logger.info("  Time: %s to %s", trades["timestamp"].iloc[0], trades["timestamp"].iloc[-1])
                logger.info("  Price: $%.2f - $%.2f", trades["price"].min(), trades["price"].max())

    logger.info("\nDone! Data saved to %s", args.output_dir)


if __name__ == "__main__":
    main()
