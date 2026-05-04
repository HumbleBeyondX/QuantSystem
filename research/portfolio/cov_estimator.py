"""Covariance estimation with Ledoit-Wolf shrinkage.

Wraps ``sklearn.covariance.LedoitWolf`` to provide an annualized cov
matrix from daily returns. Falls back to sample covariance if sklearn
isn't available or fails.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ledoit_wolf_cov(
    returns: pd.DataFrame,
    annualization_factor: int = 252,
    fallback_sample: bool = True,
) -> pd.DataFrame:
    """Ledoit-Wolf shrunk covariance, annualized.

    Args:
        returns: daily returns DataFrame, columns are tickers, index is date.
                 NaN rows / cols handled gracefully (NaN tickers are dropped).
        annualization_factor: 252 for daily.
        fallback_sample: if sklearn fails, use simple sample covariance.

    Returns:
        DataFrame (tickers × tickers) annualized covariance.
    """
    if returns.empty:
        return pd.DataFrame()

    df = returns.dropna(axis=1, how="all").dropna(axis=0, how="any")
    if df.empty or df.shape[1] < 2:
        # Degenerate — return diagonal sample variance
        return (returns.var() * annualization_factor).to_frame().pipe(
            lambda v: pd.DataFrame(np.diag(v.iloc[:, 0]), index=v.index, columns=v.index)
        )

    tickers = df.columns.tolist()

    try:
        from sklearn.covariance import LedoitWolf
        lw = LedoitWolf()
        lw.fit(df.values)
        cov = lw.covariance_ * annualization_factor
        return pd.DataFrame(cov, index=tickers, columns=tickers)
    except Exception:
        if not fallback_sample:
            raise
        cov = df.cov().values * annualization_factor
        return pd.DataFrame(cov, index=tickers, columns=tickers)


def compute_returns_panel(
    closes_wide: pd.DataFrame,
    method: str = "log",
) -> pd.DataFrame:
    """Daily returns DataFrame from wide close prices.

    Args:
        closes_wide: index=date, columns=tickers, values=close.
        method: 'log' or 'simple'.
    """
    if method == "log":
        return np.log(closes_wide / closes_wide.shift(1))
    return closes_wide.pct_change()
