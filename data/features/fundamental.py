"""Fundamental / cross-sectional factor features for equity strategies.

Value, momentum, quality, volatility, and size factors for stock selection.
"""

from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd

from core.logging import get_logger

logger = get_logger(__name__)


# ============================================================
# Momentum Factors
# ============================================================


def compute_momentum(df: pd.DataFrame, period: int = 21) -> pd.Series:
    """Price momentum: return over lookback period."""
    return df["close"].pct_change(period)


def compute_momentum_12_1(df: pd.DataFrame) -> pd.Series:
    """Classic 12-month momentum with 1-month reversal skip.

    For daily data: ~252 days momentum, skip last ~21 days.
    """
    return df["close"].shift(21).pct_change(231)


def compute_rate_of_change(df: pd.DataFrame, period: int = 10) -> pd.Series:
    """Rate of Change (ROC)."""
    return (df["close"] / df["close"].shift(period) - 1) * 100


# ============================================================
# Volatility Factors
# ============================================================


def compute_realized_vol(df: pd.DataFrame, period: int = 21) -> pd.Series:
    """Realized volatility (annualized std of returns)."""
    returns = df["close"].pct_change()
    return returns.rolling(window=period).std() * np.sqrt(252)


def compute_vol_of_vol(df: pd.DataFrame, period: int = 21, outer: int = 63) -> pd.Series:
    """Volatility of volatility."""
    vol = compute_realized_vol(df, period)
    return vol.rolling(window=outer).std()


def compute_downside_vol(df: pd.DataFrame, period: int = 21) -> pd.Series:
    """Downside volatility (semi-deviation)."""
    returns = df["close"].pct_change()
    downside = returns.where(returns < 0, 0)
    return downside.rolling(window=period).std() * np.sqrt(252)


# ============================================================
# Mean Reversion Factors
# ============================================================


def compute_rsi_factor(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """RSI as a mean reversion factor (centered around 50)."""
    from data.features.technical import compute_rsi
    return compute_rsi(df["close"], period) - 50


def compute_zscore(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Z-score: (price - SMA) / std."""
    sma = df["close"].rolling(window=period).mean()
    std = df["close"].rolling(window=period).std()
    return (df["close"] - sma) / std.replace(0, np.nan)


def compute_distance_from_high(df: pd.DataFrame, period: int = 52 * 5) -> pd.Series:
    """Distance from N-period high (drawdown indicator)."""
    rolling_high = df["close"].rolling(window=period).max()
    return (df["close"] / rolling_high) - 1


# ============================================================
# Volume / Liquidity Factors
# ============================================================


def compute_illiquidity(df: pd.DataFrame, period: int = 21) -> pd.Series:
    """Amihud illiquidity ratio: |return| / dollar_volume."""
    returns = df["close"].pct_change().abs()
    dollar_vol = df["close"] * df["volume"]
    ratio = returns / dollar_vol.replace(0, np.nan)
    return ratio.rolling(window=period).mean()


def compute_turnover(df: pd.DataFrame, period: int = 21) -> pd.Series:
    """Average turnover (volume / shares_outstanding proxy: use rolling avg volume)."""
    return df["volume"].rolling(window=period).mean()


# ============================================================
# Feature Registry
# ============================================================

FUNDAMENTAL_FEATURES: Dict[str, tuple] = {
    "momentum_21": (lambda df: compute_momentum(df, 21), "momentum", 21),
    "momentum_63": (lambda df: compute_momentum(df, 63), "momentum", 63),
    "momentum_252": (lambda df: compute_momentum(df, 252), "momentum", 252),
    "momentum_12_1": (compute_momentum_12_1, "momentum", 273),
    "roc_10": (lambda df: compute_rate_of_change(df, 10), "momentum", 10),
    "realized_vol_21": (lambda df: compute_realized_vol(df, 21), "volatility", 21),
    "realized_vol_63": (lambda df: compute_realized_vol(df, 63), "volatility", 63),
    "downside_vol_21": (lambda df: compute_downside_vol(df, 21), "volatility", 21),
    "vol_of_vol": (lambda df: compute_vol_of_vol(df), "volatility", 84),
    "zscore_20": (lambda df: compute_zscore(df, 20), "mean_reversion", 20),
    "rsi_factor": (lambda df: compute_rsi_factor(df), "mean_reversion", 14),
    "distance_from_high": (lambda df: compute_distance_from_high(df, 260), "mean_reversion", 260),
    "illiquidity": (lambda df: compute_illiquidity(df), "liquidity", 21),
    "turnover_21": (lambda df: compute_turnover(df), "liquidity", 21),
}


def register_fundamental_features(store) -> None:
    """Register all fundamental features with a FeatureStore instance."""
    for name, (func, category, lookback) in FUNDAMENTAL_FEATURES.items():
        store.register(
            name=name,
            func=func,
            category=f"fundamental_{category}",
            lookback=lookback,
        )
