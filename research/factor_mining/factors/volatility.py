"""Volatility / risk factors.

Convention reminder: HIGHER value = MORE attractive long. Since the
"low vol anomaly" says low-vol stocks outperform, we sign-flip vol
factors so LOW vol → HIGH score.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factor_mining.base import CrossSectionalFactor, register_factor
from research.factor_mining.data_panel import DataPanel
from research.factor_mining.operators import (
    realized_vol,
    ts_std,
    beta as ts_beta,
)


# ────────────────────────────────────────────────────────────
@register_factor
class RealizedVol_21(CrossSectionalFactor):
    """21-day annualized realized vol, sign-flipped (low vol = high score)."""
    name = "realized_vol_21"
    category = "volatility"
    universe_type = "multi"
    lookback_days = 21
    description = "21-day annualized realized vol (sign-flipped: low vol = high score)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        return -realized_vol(panel.log_returns_wide, 21)


# ────────────────────────────────────────────────────────────
@register_factor
class RealizedVol_63(CrossSectionalFactor):
    name = "realized_vol_63"
    category = "volatility"
    universe_type = "multi"
    lookback_days = 63
    description = "63-day annualized realized vol (sign-flipped)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        return -realized_vol(panel.log_returns_wide, 63)


# ────────────────────────────────────────────────────────────
@register_factor
class RealizedVol_252(CrossSectionalFactor):
    name = "realized_vol_252"
    category = "volatility"
    universe_type = "multi"
    lookback_days = 252
    description = "252-day annualized realized vol (sign-flipped)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        return -realized_vol(panel.log_returns_wide, 252)


# ────────────────────────────────────────────────────────────
@register_factor
class DownsideVol_63(CrossSectionalFactor):
    """Semi-deviation: std of negative returns only.
    Captures asymmetric downside risk. Sign-flipped."""
    name = "downside_vol_63"
    category = "volatility"
    universe_type = "multi"
    lookback_days = 63
    description = "63-day semi-deviation (downside vol, sign-flipped)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.log_returns_wide
        neg = ret.where(ret < 0)
        return -(neg.rolling(63, min_periods=30).std() * np.sqrt(252))


# ────────────────────────────────────────────────────────────
@register_factor
class IdioVol_63(CrossSectionalFactor):
    """Idiosyncratic volatility: residual std after market regression.

    Ang, Hodrick, Xing, Zhang (2006) — high IV stocks underperform.
    Sign-flipped so low-IV → high-score.
    """
    name = "idio_vol_63"
    category = "volatility"
    universe_type = "multi"
    lookback_days = 63
    description = "63-day idiosyncratic vol after market regression (sign-flipped)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.log_returns_wide
        market = ret.mean(axis=1)
        n = 63
        out = pd.DataFrame(index=ret.index, columns=ret.columns, dtype="float64")
        for col in ret.columns:
            r = ret[col]
            cov = r.rolling(n, min_periods=30).cov(market)
            var_m = market.rolling(n, min_periods=30).var().replace(0, np.nan)
            beta_t = cov / var_m
            # Residuals = r - alpha - beta * market
            mean_r = r.rolling(n, min_periods=30).mean()
            mean_m = market.rolling(n, min_periods=30).mean()
            alpha = mean_r - beta_t * mean_m
            # Approximate residual std as: total var − beta² × market var
            total_var = r.rolling(n, min_periods=30).var()
            resid_var = (total_var - beta_t ** 2 * var_m).clip(lower=0)
            out[col] = -np.sqrt(resid_var * 252)
        return out


# ────────────────────────────────────────────────────────────
@register_factor
class VolOfVol_63(CrossSectionalFactor):
    """Std of rolling 21-day vol over the last 63 days. Sign-flipped:
    LOW vol-of-vol = stable regime = HIGH score."""
    name = "vol_of_vol_63"
    category = "volatility"
    universe_type = "multi"
    lookback_days = 63
    description = "Std of rolling 21d vol over last 63d (sign-flipped)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.log_returns_wide
        rolling_vol = ts_std(ret, 21) * np.sqrt(252)
        return -ts_std(rolling_vol, 63)


# ────────────────────────────────────────────────────────────
@register_factor
class MaxRet_21(CrossSectionalFactor):
    """Bali, Cakici, Whitelaw (2011): MAX = average of top-5 daily
    returns over 21 days. High MAX (lottery-like) underperforms.
    Sign-flipped: low MAX = high score."""
    name = "max_ret_21"
    category = "volatility"
    universe_type = "multi"
    lookback_days = 21
    description = "Avg of top-5 daily returns over 21d, sign-flipped (Bali MAX anomaly)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.returns_wide
        # For each (date, ticker), top-5 in last 21d
        out = pd.DataFrame(index=ret.index, columns=ret.columns, dtype="float64")

        def _top5_mean(arr: np.ndarray) -> float:
            valid = arr[~np.isnan(arr)]
            if len(valid) < 10:
                return np.nan
            top5 = np.sort(valid)[-5:]
            return float(top5.mean())

        for col in ret.columns:
            out[col] = ret[col].rolling(21, min_periods=10).apply(_top5_mean, raw=True)
        return -out
