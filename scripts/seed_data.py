#!/usr/bin/env python3
"""Seed sample OHLCV data for local development and testing.

Downloads historical data via *yfinance* (equities) and *ccxt* (crypto),
saves CSV files to ``data/sample/``, and optionally inserts into TimescaleDB.

Usage examples::

    python scripts/seed_data.py
    python scripts/seed_data.py --symbols SPY AAPL --start-date 2024-01-01
    python scripts/seed_data.py --symbols BTC/USDT --start-date 2024-06-01 --insert-db
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import pandas as pd

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "data" / "sample"

DEFAULT_EQUITY_SYMBOLS = ["SPY", "AAPL", "MSFT"]
DEFAULT_CRYPTO_SYMBOLS = ["BTC/USDT"]
DEFAULT_START = "2024-01-01"
DEFAULT_END = datetime.utcnow().strftime("%Y-%m-%d")

DB_CONFIG = {
    "host": os.getenv("TIMESCALE_HOST", "localhost"),
    "port": int(os.getenv("TIMESCALE_PORT", "5432")),
    "database": os.getenv("TIMESCALE_DB", "quantsystem"),
    "user": os.getenv("TIMESCALE_USER", "quant"),
    "password": os.getenv("TIMESCALE_PASSWORD", "quant"),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("seed_data")

# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------


def _is_crypto(symbol: str) -> bool:
    return "/" in symbol


def fetch_equity(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Download equity OHLCV using yfinance."""
    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance is not installed. Run: pip install yfinance")
        sys.exit(1)

    logger.info("Downloading equity data for %s (%s -> %s)", symbol, start, end)
    ticker = yf.Ticker(symbol)
    df = ticker.history(start=start, end=end, auto_adjust=True)
    if df.empty:
        logger.warning("No data returned for %s", symbol)
        return df

    df = df.rename(columns=str.lower)
    df.index.name = "timestamp"
    # Keep standard OHLCV columns only
    cols = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    return df[cols].copy()


def fetch_crypto(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Download crypto OHLCV using ccxt (Binance by default)."""
    try:
        import ccxt
    except ImportError:
        logger.error("ccxt is not installed. Run: pip install ccxt")
        sys.exit(1)

    logger.info("Downloading crypto data for %s (%s -> %s)", symbol, start, end)
    exchange = ccxt.binance({"enableRateLimit": True})

    since_ms = int(datetime.strptime(start, "%Y-%m-%d").timestamp() * 1000)
    end_ms = int(datetime.strptime(end, "%Y-%m-%d").timestamp() * 1000)

    all_candles: list = []
    while since_ms < end_ms:
        candles = exchange.fetch_ohlcv(symbol, timeframe="1d", since=since_ms, limit=1000)
        if not candles:
            break
        all_candles.extend(candles)
        since_ms = candles[-1][0] + 1  # advance past last candle

    if not all_candles:
        logger.warning("No data returned for %s", symbol)
        return pd.DataFrame()

    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp")
    # Filter to requested date range
    df = df.loc[:end]
    return df


def fetch_symbol(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Route to the appropriate fetcher."""
    if _is_crypto(symbol):
        return fetch_crypto(symbol, start, end)
    return fetch_equity(symbol, start, end)

# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_csv(symbol: str, df: pd.DataFrame) -> Path:
    """Save a DataFrame to CSV in the sample data directory."""
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = symbol.replace("/", "_").replace(" ", "_")
    path = SAMPLE_DIR / f"{safe_name}.csv"
    df.to_csv(path)
    logger.info("Saved %d rows to %s", len(df), path)
    return path


async def insert_into_db(symbol: str, df: pd.DataFrame) -> None:
    """Bulk-insert OHLCV rows into TimescaleDB."""
    try:
        import asyncpg
    except ImportError:
        logger.error("asyncpg is not installed. Run: pip install asyncpg")
        return

    conn = await asyncpg.connect(**DB_CONFIG)
    try:
        rows = [
            (symbol, ts.to_pydatetime(), row.open, row.high, row.low, row.close, row.volume)
            for ts, row in df.iterrows()
        ]
        await conn.executemany(
            """
            INSERT INTO ohlcv (symbol, timestamp, open, high, low, close, volume)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT DO NOTHING
            """,
            rows,
        )
        logger.info("Inserted %d rows for %s into TimescaleDB", len(rows), symbol)
    except Exception as exc:
        logger.error("DB insert failed for %s: %s", symbol, exc)
    finally:
        await conn.close()

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed sample OHLCV data")
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=DEFAULT_EQUITY_SYMBOLS + DEFAULT_CRYPTO_SYMBOLS,
        help="Symbols to download (use '/' for crypto, e.g. BTC/USDT)",
    )
    parser.add_argument("--start-date", default=DEFAULT_START, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", default=DEFAULT_END, help="End date (YYYY-MM-DD)")
    parser.add_argument(
        "--insert-db",
        action="store_true",
        default=False,
        help="Also insert data into TimescaleDB",
    )
    return parser.parse_args()


async def _async_main(args: argparse.Namespace) -> None:
    for symbol in args.symbols:
        df = fetch_symbol(symbol, args.start_date, args.end_date)
        if df.empty:
            continue
        save_csv(symbol, df)
        if args.insert_db:
            await insert_into_db(symbol, df)


def main() -> None:
    args = parse_args()
    logger.info("Seeding data for symbols: %s", args.symbols)
    asyncio.run(_async_main(args))
    logger.info("Done.")


if __name__ == "__main__":
    main()
