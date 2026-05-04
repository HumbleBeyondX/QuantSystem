"""Macro feature engineering for the regime classifier.

Inputs
------
Long-format panel DataFrame with columns:
  ['timestamp', 'ticker', 'open', 'high', 'low', 'close', 'volume']

Required tickers in the panel:
  SPY, ^VIX, ^TNX, ^IRX, HYG, IEF + the sector ETFs

Output
------
Wide-format daily features indexed by date with these 6 columns:

  spy_ret_20d        — SPY log-return over trailing 20 days
  vix                — VIX close (level)
  term_spread        — ^TNX - ^IRX  (10y minus 3-month)
  credit_spread_z    — z-score of HYG/IEF ratio over trailing 252 days.
                       Lower (more negative) = WIDER credit spread = stress
  spy_realized_vol   — annualized 60-day realized vol of SPY (decimal, e.g. 0.18)
  sector_dispersion  — std across sector-ETF 20-day returns,
                       a proxy for cross-sectional opportunity

The returned frame can be fed directly to GMMRegimeDetector.fit/predict.
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np
import pandas as pd


SECTOR_ETFS_FOR_DISPERSION: List[str] = [
    "XLK", "XLF", "XLV", "XLY", "XLP",
    "XLE", "XLI", "XLU", "XLB",
    # XLRE and XLC come in late and would mess up early dispersion;
    # using only the 9 sectors that exist 2010-onward.
]


def _pivot_close(panel: pd.DataFrame) -> pd.DataFrame:
    """Return wide DataFrame indexed by timestamp, columns are tickers."""
    if "timestamp" not in panel.columns or "ticker" not in panel.columns:
        raise ValueError("panel must have 'timestamp' and 'ticker' columns")
    wide = panel.pivot_table(
        index="timestamp", columns="ticker", values="close", aggfunc="last",
    )
    return wide.sort_index()


def compute_macro_features(panel: pd.DataFrame) -> pd.DataFrame:
    """Compute the 6 daily regime features from the multi-ticker panel.

    Robust to missing tickers — features that depend on absent tickers
    return NaN columns rather than raising. Drop NaN rows before feeding
    to GMM.
    """
    closes = _pivot_close(panel)

    feat = pd.DataFrame(index=closes.index)

    # 1) SPY 20-day log return
    if "SPY" in closes:
        spy = closes["SPY"]
        feat["spy_ret_20d"] = np.log(spy / spy.shift(20))
    else:
        feat["spy_ret_20d"] = np.nan

    # 2) VIX level
    feat["vix"] = closes["^VIX"] if "^VIX" in closes else np.nan

    # 3) Term spread = 10y − 3m
    if "^TNX" in closes and "^IRX" in closes:
        feat["term_spread"] = closes["^TNX"] - closes["^IRX"]
    else:
        feat["term_spread"] = np.nan

    # 4) Credit spread z-score from HYG/IEF ratio (lower = wider = stress)
    if "HYG" in closes and "IEF" in closes:
        ratio = closes["HYG"] / closes["IEF"]
        roll_mean = ratio.rolling(252, min_periods=126).mean()
        roll_std = ratio.rolling(252, min_periods=126).std().replace(0, np.nan)
        feat["credit_spread_z"] = (ratio - roll_mean) / roll_std
    else:
        feat["credit_spread_z"] = np.nan

    # 5) SPY 60-day annualized realized vol
    if "SPY" in closes:
        log_ret = np.log(closes["SPY"] / closes["SPY"].shift(1))
        feat["spy_realized_vol"] = log_ret.rolling(60, min_periods=30).std() * np.sqrt(252)
    else:
        feat["spy_realized_vol"] = np.nan

    # 6) Cross-sectional sector dispersion (std of 20d returns across sectors)
    avail = [t for t in SECTOR_ETFS_FOR_DISPERSION if t in closes.columns]
    if avail:
        sector_panel = closes[avail]
        sector_20d_ret = np.log(sector_panel / sector_panel.shift(20))
        feat["sector_dispersion"] = sector_20d_ret.std(axis=1, skipna=True)
    else:
        feat["sector_dispersion"] = np.nan

    return feat


def feature_names() -> List[str]:
    return [
        "spy_ret_20d", "vix", "term_spread",
        "credit_spread_z", "spy_realized_vol", "sector_dispersion",
    ]
