"""Primitive operators for cross-sectional factor construction.

Conventions
-----------
All operators take and return wide DataFrames (date × ticker). Time runs
along the index (vertical), tickers along columns (horizontal).

  * Cross-sectional ops act per-row (across tickers at a single date).
  * Time-series ops act per-column (along time, per ticker).

These compose into Alpha101-style formulas. Example:

    rank(ts_corr(volume, close, 5))   # rank-of(rolling 5d corr)

Naming follows WorldQuant conventions where reasonable.
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


# ============================================================
# CROSS-SECTIONAL (per row)
# ============================================================

def rank(x: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional rank, normalized to [0, 1] per row.

    Higher input → higher rank. NaN rows preserved as NaN.
    """
    return x.rank(axis=1, pct=True, method="average")


def scale(x: pd.DataFrame, total: float = 1.0) -> pd.DataFrame:
    """Rescale each row so sum of |values| = total.

    Common WorldQuant move; useful for portfolio-style factors.
    """
    abs_sum = x.abs().sum(axis=1).replace(0, np.nan)
    return x.div(abs_sum, axis=0) * total


def cs_zscore(x: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score per row."""
    mean = x.mean(axis=1)
    std = x.std(axis=1).replace(0, np.nan)
    return x.sub(mean, axis=0).div(std, axis=0)


def cs_demean(x: pd.DataFrame) -> pd.DataFrame:
    """Subtract cross-sectional mean per row (mean-zero each day)."""
    return x.sub(x.mean(axis=1), axis=0)


def neutralize(
    x: pd.DataFrame,
    group_factor: pd.DataFrame,
) -> pd.DataFrame:
    """Sector- or group-neutralize: subtract group mean per row.

    group_factor must align with x (same dates, same tickers, but values
    are group labels e.g. sector codes).
    """
    out = x.copy()
    for date, row in x.iterrows():
        if date not in group_factor.index:
            continue
        groups = group_factor.loc[date]
        df = pd.DataFrame({"v": row, "g": groups}).dropna()
        if df.empty:
            continue
        means = df.groupby("g")["v"].transform("mean")
        out.loc[date, df.index] = (df["v"] - means).values
    return out


# ============================================================
# TIME-SERIES (per column)
# ============================================================

def delta(x: pd.DataFrame, n: int) -> pd.DataFrame:
    """x[t] − x[t-n] per ticker."""
    return x - x.shift(n)


def delay(x: pd.DataFrame, n: int) -> pd.DataFrame:
    """x[t-n]."""
    return x.shift(n)


def ts_max(x: pd.DataFrame, n: int) -> pd.DataFrame:
    return x.rolling(n, min_periods=max(2, n // 2)).max()


def ts_min(x: pd.DataFrame, n: int) -> pd.DataFrame:
    return x.rolling(n, min_periods=max(2, n // 2)).min()


def ts_sum(x: pd.DataFrame, n: int) -> pd.DataFrame:
    return x.rolling(n, min_periods=max(2, n // 2)).sum()


def ts_mean(x: pd.DataFrame, n: int) -> pd.DataFrame:
    return x.rolling(n, min_periods=max(2, n // 2)).mean()


def ts_std(x: pd.DataFrame, n: int) -> pd.DataFrame:
    return x.rolling(n, min_periods=max(2, n // 2)).std()


def ts_argmax(x: pd.DataFrame, n: int) -> pd.DataFrame:
    """Position of max value within the last n bars (0 = oldest, n-1 = current)."""
    return x.rolling(n, min_periods=max(2, n // 2)).apply(np.argmax, raw=True)


def ts_argmin(x: pd.DataFrame, n: int) -> pd.DataFrame:
    return x.rolling(n, min_periods=max(2, n // 2)).apply(np.argmin, raw=True)


def ts_rank(x: pd.DataFrame, n: int) -> pd.DataFrame:
    """Rolling rank: at each t, what is x[t]'s rank among the last n values?
    Returns value in [0, 1] (pct rank)."""
    return x.rolling(n, min_periods=max(2, n // 2)).rank(pct=True)


def ts_corr(x: pd.DataFrame, y: pd.DataFrame, n: int) -> pd.DataFrame:
    """Rolling correlation of x and y (per ticker). Both must align."""
    return x.rolling(n, min_periods=max(3, n // 2)).corr(y)


def ts_cov(x: pd.DataFrame, y: pd.DataFrame, n: int) -> pd.DataFrame:
    return x.rolling(n, min_periods=max(3, n // 2)).cov(y)


# ============================================================
# DECAY / WEIGHTING
# ============================================================

def decay_linear(x: pd.DataFrame, n: int) -> pd.DataFrame:
    """Linearly weighted moving average: weights 1, 2, ..., n; older = lower."""
    weights = np.arange(1, n + 1, dtype=float)
    weights /= weights.sum()

    def _wma(arr: np.ndarray) -> float:
        if np.isnan(arr).any():
            mask = ~np.isnan(arr)
            if mask.sum() < n // 2:
                return np.nan
            w = weights[mask]
            w = w / w.sum()
            return float(np.dot(arr[mask], w))
        return float(np.dot(arr, weights))

    return x.rolling(n, min_periods=max(2, n // 2)).apply(_wma, raw=True)


def decay_exp(x: pd.DataFrame, half_life: int) -> pd.DataFrame:
    """EWMA decay."""
    return x.ewm(halflife=half_life, min_periods=max(2, half_life)).mean()


# ============================================================
# MATHEMATICAL
# ============================================================

def signedpower(x: pd.DataFrame, p: float = 2.0) -> pd.DataFrame:
    """sign(x) × |x|^p — common WorldQuant nonlinearity."""
    return np.sign(x) * (x.abs() ** p)


def log_safe(x: pd.DataFrame, eps: float = 1e-9) -> pd.DataFrame:
    """log(|x| + eps), preserves sign via sign(x) × log(|x|+eps)."""
    return np.sign(x) * np.log(x.abs() + eps)


def winsorize(
    x: pd.DataFrame,
    low: float = 0.01,
    high: float = 0.99,
) -> pd.DataFrame:
    """Cross-sectional winsorize: clip per row at `low` and `high` quantiles."""
    out = x.copy()
    for ts, row in x.iterrows():
        valid = row.dropna()
        if len(valid) < 5:
            continue
        ql, qh = valid.quantile([low, high])
        out.loc[ts] = row.clip(ql, qh)
    return out


def safe_divide(x: pd.DataFrame, y: pd.DataFrame, eps: float = 1e-12) -> pd.DataFrame:
    """x / y with NaN where y is too close to zero."""
    return x.div(y.where(y.abs() > eps, np.nan))


# ============================================================
# Helpers used by factor implementations
# ============================================================

def realized_vol(returns: pd.DataFrame, n: int, annualize: int = 252) -> pd.DataFrame:
    """Annualized realized volatility from returns."""
    return ts_std(returns, n) * np.sqrt(annualize)


def beta(
    returns: pd.DataFrame,
    benchmark: pd.Series,
    n: int = 252,
) -> pd.DataFrame:
    """Rolling beta vs a benchmark series (e.g. SPY returns)."""
    bench_aligned = benchmark.reindex(returns.index)
    out = pd.DataFrame(index=returns.index, columns=returns.columns, dtype="float64")
    var_b = bench_aligned.rolling(n, min_periods=max(60, n // 2)).var()
    for col in returns.columns:
        cov = returns[col].rolling(n, min_periods=max(60, n // 2)).cov(bench_aligned)
        out[col] = cov / var_b.replace(0, np.nan)
    return out
