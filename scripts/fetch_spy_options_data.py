"""Fetch real SPY + VIX + Risk-Free Rate data for Gamma Scalp backtest.

Data sources (all from Yahoo Finance via yfinance):
  - SPY: S&P 500 ETF daily OHLCV
  - ^VIX: CBOE Volatility Index (proxy for SPY ATM implied vol)
  - ^IRX: 13-Week Treasury Bill rate (risk-free rate proxy)

Usage:
    python scripts/fetch_spy_options_data.py --start 2023-01-01 --end 2026-03-15
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def fetch_with_yfinance(
    symbols: list[str],
    start: str,
    end: str,
    interval: str = "1d",
) -> dict[str, pd.DataFrame]:
    """Fetch OHLCV data from Yahoo Finance."""
    try:
        import yfinance as yf
    except ImportError:
        print("Installing yfinance...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "yfinance", "-q"])
        import yfinance as yf

    results = {}
    for sym in symbols:
        print(f"  Fetching {sym} ({start} to {end}, interval={interval})...")
        ticker = yf.Ticker(sym)
        df = ticker.history(start=start, end=end, interval=interval)
        if df.empty:
            print(f"  WARNING: No data for {sym}")
            continue
        # Normalize column names
        df.columns = [c.lower().replace(" ", "_") for c in df.columns]
        df.index.name = "timestamp"
        df = df.reset_index()
        # Convert timezone-aware to naive UTC
        if hasattr(df["timestamp"].dtype, "tz") and df["timestamp"].dt.tz is not None:
            df["timestamp"] = df["timestamp"].dt.tz_localize(None)
        results[sym] = df
        print(f"  Got {len(df)} rows for {sym} ({df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]})")
    return results


def merge_spy_vix_irx(
    spy_df: pd.DataFrame,
    vix_df: pd.DataFrame,
    irx_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Merge SPY OHLCV with VIX (implied vol) and IRX (risk-free rate).

    Output columns:
        timestamp, open, high, low, close, volume,
        vix_close (VIX level), implied_vol (VIX/100),
        risk_free_rate (IRX/100 or default 0.05)
    """
    # SPY base
    df = spy_df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.normalize()

    # VIX
    vix = vix_df[["timestamp", "close"]].copy()
    vix.columns = ["timestamp", "vix_close"]
    vix["timestamp"] = pd.to_datetime(vix["timestamp"]).dt.normalize()
    df = df.merge(vix, on="timestamp", how="left")
    df["vix_close"] = df["vix_close"].ffill()
    df["implied_vol"] = df["vix_close"] / 100.0

    # Risk-free rate
    if irx_df is not None and len(irx_df) > 0:
        irx = irx_df[["timestamp", "close"]].copy()
        irx.columns = ["timestamp", "irx_close"]
        irx["timestamp"] = pd.to_datetime(irx["timestamp"]).dt.normalize()
        df = df.merge(irx, on="timestamp", how="left")
        df["irx_close"] = df["irx_close"].ffill()
        df["risk_free_rate"] = df["irx_close"] / 100.0
    else:
        df["risk_free_rate"] = 0.05

    # Fill any remaining NaN
    df["implied_vol"] = df["implied_vol"].fillna(0.20)
    df["risk_free_rate"] = df["risk_free_rate"].fillna(0.05)

    # Sort and clean
    df = df.sort_values("timestamp").reset_index(drop=True)
    df = df.dropna(subset=["close"])

    return df


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add pre-computed features for the gamma scalp strategy.

    Features:
        - realized_vol_20d: 20-day Parkinson (high-low) realized vol (annualized)
        - realized_vol_5d:  5-day fast realized vol
        - rv_iv_ratio:      realized_vol_20d / implied_vol
        - rv_iv_spread:     realized_vol_20d - implied_vol
        - vol_of_vol:       20-day rolling std of daily VIX changes
        - vix_sma_20:       20-day SMA of VIX (vol regime)
        - vix_percentile:   252-day percentile rank of VIX
        - daily_return:     Close-to-close return
        - intraday_range:   (high - low) / close
        - gap_pct:          Overnight gap (open / prev_close - 1)
    """
    # Parkinson realized vol (more efficient than close-to-close)
    hl_log_sq = np.log(df["high"] / df["low"]) ** 2
    for window in [5, 10, 20]:
        parkinson_var = hl_log_sq.rolling(window).mean() / (4 * np.log(2))
        df[f"realized_vol_{window}d"] = np.sqrt(parkinson_var * 252)

    # Yang-Zhang estimator (20d) - combines overnight + intraday vol
    log_oc = np.log(df["close"] / df["open"])
    log_co = np.log(df["open"] / df["close"].shift(1))
    for window in [20]:
        var_co = log_co.rolling(window).var()
        var_oc = log_oc.rolling(window).var()
        # Rogers-Satchell component
        log_ho = np.log(df["high"] / df["open"])
        log_lo = np.log(df["low"] / df["open"])
        log_hc = np.log(df["high"] / df["close"])
        log_lc = np.log(df["low"] / df["close"])
        rs_var = (log_ho * log_hc + log_lo * log_lc).rolling(window).mean()
        # Yang-Zhang combination
        k = 0.34 / (1.34 + (window + 1) / (window - 1))
        yz_var = var_co + k * var_oc + (1 - k) * rs_var
        df[f"yz_realized_vol_{window}d"] = np.sqrt(np.maximum(yz_var, 0) * 252)

    # RV-IV metrics
    df["rv_iv_ratio"] = df["realized_vol_20d"] / df["implied_vol"]
    df["rv_iv_spread"] = df["realized_vol_20d"] - df["implied_vol"]

    # Vol-of-Vol (uncertainty in vol regime)
    df["vix_daily_change"] = df["vix_close"].pct_change()
    df["vol_of_vol_20d"] = df["vix_daily_change"].rolling(20).std() * np.sqrt(252)

    # VIX regime indicators
    df["vix_sma_20"] = df["vix_close"].rolling(20).mean()
    df["vix_percentile_252d"] = df["vix_close"].rolling(252, min_periods=60).apply(
        lambda x: (x.iloc[-1] > x).mean(), raw=False
    )

    # Price features
    df["daily_return"] = df["close"].pct_change()
    df["intraday_range"] = (df["high"] - df["low"]) / df["close"]
    df["gap_pct"] = df["open"] / df["close"].shift(1) - 1

    # Realized vol momentum (is RV accelerating?)
    df["rv_momentum"] = df["realized_vol_5d"] - df["realized_vol_20d"]

    # Forward-fill NaN for rolling windows
    return df


def main():
    parser = argparse.ArgumentParser(description="Fetch SPY/VIX/IRX data for gamma scalp")
    parser.add_argument("--start", type=str, default="2023-01-01", help="Start date")
    parser.add_argument("--end", type=str, default="2026-03-15", help="End date")
    parser.add_argument("--interval", type=str, default="1d", help="Bar interval")
    parser.add_argument("--output", type=str, default=None, help="Output CSV path")
    args = parser.parse_args()

    print("=" * 60)
    print("SPY Gamma Scalp Data Fetcher")
    print("=" * 60)

    # Fetch data
    print("\n[1/4] Fetching market data from Yahoo Finance...")
    data = fetch_with_yfinance(
        symbols=["SPY", "^VIX", "^IRX"],
        start=args.start,
        end=args.end,
        interval=args.interval,
    )

    if "SPY" not in data:
        print("ERROR: Failed to fetch SPY data!")
        sys.exit(1)
    if "^VIX" not in data:
        print("ERROR: Failed to fetch VIX data!")
        sys.exit(1)

    # Merge
    print("\n[2/4] Merging SPY + VIX + IRX...")
    df = merge_spy_vix_irx(
        spy_df=data["SPY"],
        vix_df=data["^VIX"],
        irx_df=data.get("^IRX"),
    )
    print(f"  Merged dataset: {len(df)} rows")
    print(f"  Date range: {df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]}")
    print(f"  SPY range: ${df['close'].min():.2f} - ${df['close'].max():.2f}")
    print(f"  VIX range: {df['vix_close'].min():.2f} - {df['vix_close'].max():.2f}")

    # Compute features
    print("\n[3/4] Computing features (RV, vol regime, etc.)...")
    df = compute_features(df)

    # Summary stats
    valid = df.dropna(subset=["realized_vol_20d"])
    print(f"  Rows with valid 20d RV: {len(valid)}")
    print(f"  Avg RV(20d): {valid['realized_vol_20d'].mean():.4f}")
    print(f"  Avg IV (VIX/100): {valid['implied_vol'].mean():.4f}")
    print(f"  Avg RV/IV ratio: {valid['rv_iv_ratio'].mean():.4f}")
    print(f"  Days with RV > IV: {(valid['rv_iv_ratio'] > 1.0).sum()} / {len(valid)}")

    # Save
    output_dir = ROOT / "data" / "sample"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output) if args.output else output_dir / "SPY_gamma_scalp_daily.csv"

    print(f"\n[4/4] Saving to {output_path}...")
    df.to_csv(output_path, index=False)
    print(f"  Saved {len(df)} rows, {len(df.columns)} columns")
    print(f"  Columns: {list(df.columns)}")

    print("\n" + "=" * 60)
    print("DONE! Data ready for gamma scalp backtest.")
    print(f"  File: {output_path}")
    print(f"  Size: {output_path.stat().st_size / 1024:.1f} KB")
    print("=" * 60)


if __name__ == "__main__":
    main()
