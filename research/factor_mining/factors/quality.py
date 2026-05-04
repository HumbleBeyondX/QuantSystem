"""Quality factors — risk-adjusted return characteristics.

Without fundamental data (Compustat earnings/ROE/etc.) we proxy
"quality" via return-distribution properties: stable, smooth,
positively skewed return series score higher.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factor_mining.base import CrossSectionalFactor, register_factor
from research.factor_mining.data_panel import DataPanel
from research.factor_mining.operators import ts_mean, ts_std


# ────────────────────────────────────────────────────────────
@register_factor
class SharpeOfReturns_252(CrossSectionalFactor):
    """Own asset's Sharpe ratio over the last 252 days.

    High return-per-unit-risk → high quality. No risk-free subtraction
    (cross-sectional, RF is constant across tickers).
    """
    name = "sharpe_252"
    category = "quality"
    universe_type = "multi"
    lookback_days = 252
    description = "252-day Sharpe ratio (mean / std of daily returns)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.log_returns_wide
        mu = ts_mean(ret, 252)
        sigma = ts_std(ret, 252).replace(0, np.nan)
        return mu / sigma * np.sqrt(252)


# ────────────────────────────────────────────────────────────
@register_factor
class Skewness_252(CrossSectionalFactor):
    """Skew of daily returns over 252d.

    Positive skew = more upside surprises than downside = quality.
    Negative skew = lottery-ish, crash-prone.
    """
    name = "skew_252"
    category = "quality"
    universe_type = "multi"
    lookback_days = 252
    description = "252-day skewness of daily returns (positive = preferred)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.log_returns_wide
        return ret.rolling(252, min_periods=126).skew()


# ────────────────────────────────────────────────────────────
@register_factor
class Kurtosis_252(CrossSectionalFactor):
    """Negative kurtosis of returns — fat-tailed names underperform.
    Sign-flipped: low kurt = high score."""
    name = "kurt_252"
    category = "quality"
    universe_type = "multi"
    lookback_days = 252
    description = "Negative 252-day return kurtosis (fat tails penalized)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.log_returns_wide
        return -ret.rolling(252, min_periods=126).kurt()


# ────────────────────────────────────────────────────────────
@register_factor
class ReturnAutocorr_21(CrossSectionalFactor):
    """1-day autocorrelation of returns over last 21 days.

    Positive autocorr = trending behavior; negative = mean-reverting.
    Used as a 'persistence' feature.
    """
    name = "return_autocorr_21"
    category = "quality"
    universe_type = "multi"
    lookback_days = 21
    description = "Lag-1 autocorrelation of returns over last 21 days."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.log_returns_wide
        ret_lag = ret.shift(1)

        # Rolling Pearson cov(x, x_lag) / (std(x) * std(x_lag)) — vectorized
        n = 21
        min_p = max(10, n // 2)
        mean_x = ret.rolling(n, min_periods=min_p).mean()
        mean_y = ret_lag.rolling(n, min_periods=min_p).mean()
        mean_xy = (ret * ret_lag).rolling(n, min_periods=min_p).mean()
        std_x = ret.rolling(n, min_periods=min_p).std()
        std_y = ret_lag.rolling(n, min_periods=min_p).std()
        cov = mean_xy - mean_x * mean_y
        denom = (std_x * std_y).replace(0, np.nan)
        return cov / denom


# ────────────────────────────────────────────────────────────
@register_factor
class ReturnStability_252(CrossSectionalFactor):
    """1 / coefficient-of-variation of rolling 21d returns over 252d.

    Stable, predictable returns score higher. Equivalent to inverse of
    return-of-return-volatility.
    """
    name = "return_stability_252"
    category = "quality"
    universe_type = "multi"
    lookback_days = 252
    description = "1 / std-of(rolling 21d mean returns) over last 252 days."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.log_returns_wide
        rolling_mean = ts_mean(ret, 21)
        # Std of these rolling means over 252d (lower = more stable)
        stability_inv = ts_std(rolling_mean, 252).replace(0, np.nan)
        return 1.0 / stability_inv
