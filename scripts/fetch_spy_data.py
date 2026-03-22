#!/usr/bin/env python3
"""Fetch real SPY historical data from Yahoo Finance for Gamma Scalping backtest.

Downloads:
  1. SPY daily OHLCV price data
  2. VIX index (implied volatility proxy)
  3. Risk-free rate proxy (^IRX = 13-week T-bill)

Usage:
    python scripts/fetch_spy_data.py --start 2023-01-01 --end 2026-03-15
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
SAMPLE_DIR = PROJECT_ROOT / "data" / "sample"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("fetch_spy_data")


def fetch_spy(start: str, end: str) -> pd.DataFrame:
    """Fetch SPY daily OHLCV from Yahoo Finance."""
    logger.info("Fetching SPY data from %s to %s ...", start, end)
    spy = yf.download("SPY", start=start, end=end, interval="1d", auto_adjust=True)
    if spy.empty:
        raise RuntimeError("No SPY data returned from Yahoo Finance")

    # Flatten multi-index columns if present
    if isinstance(spy.columns, pd.MultiIndex):
        spy.columns = [col[0] for col in spy.columns]

    spy.index.name = "timestamp"
    spy.columns = [c.lower() for c in spy.columns]
    logger.info("  SPY: %d rows, %.2f - %.2f", len(spy), spy["low"].min(), spy["high"].max())
    return spy


def fetch_vix(start: str, end: str) -> pd.DataFrame:
    """Fetch VIX index as implied volatility proxy."""
    logger.info("Fetching VIX data ...")
    vix = yf.download("^VIX", start=start, end=end, interval="1d", auto_adjust=True)
    if vix.empty:
        logger.warning("No VIX data returned")
        return pd.DataFrame()

    if isinstance(vix.columns, pd.MultiIndex):
        vix.columns = [col[0] for col in vix.columns]

    vix.index.name = "timestamp"
    vix.columns = [c.lower() for c in vix.columns]
    # We only need the close as IV proxy
    vix = vix[["close"]].rename(columns={"close": "vix_close"})
    logger.info("  VIX: %d rows, %.2f - %.2f", len(vix), vix["vix_close"].min(), vix["vix_close"].max())
    return vix


def fetch_risk_free(start: str, end: str) -> pd.DataFrame:
    """Fetch 13-week T-bill rate as risk-free rate proxy."""
    logger.info("Fetching risk-free rate (^IRX) ...")
    irx = yf.download("^IRX", start=start, end=end, interval="1d", auto_adjust=True)
    if irx.empty:
        logger.warning("No ^IRX data, will use default 5%% risk-free rate")
        return pd.DataFrame()

    if isinstance(irx.columns, pd.MultiIndex):
        irx.columns = [col[0] for col in irx.columns]

    irx.index.name = "timestamp"
    irx.columns = [c.lower() for c in irx.columns]
    irx = irx[["close"]].rename(columns={"close": "risk_free_rate"})
    irx["risk_free_rate"] = irx["risk_free_rate"] / 100  # Convert from percentage
    logger.info("  Risk-free: %d rows, %.4f - %.4f", len(irx), irx["risk_free_rate"].min(), irx["risk_free_rate"].max())
    return irx


def main():
    parser = argparse.ArgumentParser(description="Fetch SPY + VIX + risk-free data")
    parser.add_argument("--start", default="2023-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default="2026-03-15", help="End date (YYYY-MM-DD)")
    args = parser.parse_args()

    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)

    # Fetch all data
    spy_df = fetch_spy(args.start, args.end)
    vix_df = fetch_vix(args.start, args.end)
    rf_df = fetch_risk_free(args.start, args.end)

    # Merge into a single DataFrame
    combined = spy_df.copy()
    if not vix_df.empty:
        combined = combined.join(vix_df, how="left")
        combined["vix_close"] = combined["vix_close"].ffill().bfill()
    else:
        combined["vix_close"] = 20.0  # Default IV

    if not rf_df.empty:
        combined = combined.join(rf_df, how="left")
        combined["risk_free_rate"] = combined["risk_free_rate"].ffill().bfill()
    else:
        combined["risk_free_rate"] = 0.05  # Default 5%

    # Compute realized volatility (20-day)
    combined["returns"] = combined["close"].pct_change()
    combined["realized_vol_20d"] = combined["returns"].rolling(20).std() * (252 ** 0.5)
    combined["implied_vol"] = combined["vix_close"] / 100  # VIX is annualized %

    # Save
    output_path = SAMPLE_DIR / "SPY_daily.csv"
    combined.to_csv(output_path)
    logger.info("Saved %d rows to %s", len(combined), output_path)

    # Summary
    logger.info("\n=== SPY Data Summary ===")
    logger.info("  Period: %s to %s", combined.index[0].strftime("%Y-%m-%d"), combined.index[-1].strftime("%Y-%m-%d"))
    logger.info("  Price range: $%.2f - $%.2f", combined["low"].min(), combined["high"].max())
    logger.info("  VIX range: %.1f - %.1f", combined["vix_close"].min(), combined["vix_close"].max())
    logger.info("  Avg daily volume: %.0f", combined["volume"].mean())
    logger.info("  Trading days: %d", len(combined))


if __name__ == "__main__":
    main()
