"""Volatility structure factors — regime signals embedded in variance dynamics.

Unlike simple ATR or rolling std, these capture:
  * Clustering (are volatile bars followed by more volatile bars?)
  * Asymmetry (vol spikes in down-moves vs up-moves)
  * Vol-of-vol (regime stability)

These are informative by themselves but primarily function as regime
gates — i.e. used to switch other factors on/off.

Sign convention: for regime signals we return raw magnitude (not signed),
since "high vol" is a state, not a direction. The strategy layer decides
how to act on the value.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factors.ts_factor_base import TimeSeriesFactor, register_factor


# ────────────────────────────────────────────────────────────
# 8. Realized volatility 24h (unsigned magnitude)
# ────────────────────────────────────────────────────────────

@register_factor
class RealizedVol24h(TimeSeriesFactor):
    """sqrt(sum of squared log returns over trailing 24 bars).

    Classic realized vol. Used as a regime gate; not directly predictive
    of direction. To use as a directional signal, combine with sign of
    recent momentum.
    """
    name = "realized_vol_24h"
    category = "volatility_structure"
    lookback_bars = 24
    description = "24h trailing realized volatility (unsigned)."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        log_ret = np.log(df["close"] / df["close"].shift(1))
        return np.sqrt((log_ret ** 2).rolling(24, min_periods=12).sum())


# ────────────────────────────────────────────────────────────
# 9. Vol clustering — autocorrelation of |returns|
# ────────────────────────────────────────────────────────────

@register_factor
class VolClustering48h(TimeSeriesFactor):
    """Lag-1 autocorrelation of |log-returns| over trailing 48 bars.

    High value (close to 1) → vol clusters strongly (GARCH-like regime) →
    recent vol is informative about next-bar vol. Near 0 → IID-like.
    """
    name = "vol_clustering_48h"
    category = "volatility_structure"
    lookback_bars = 48
    description = "Lag-1 autocorr of |log-return| over last 48h — vol clustering strength."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        abs_ret = np.abs(np.log(df["close"] / df["close"].shift(1)))

        # Vectorized rolling lag-1 autocorrelation:
        #   corr(x_t, x_{t-1}) = (E[x * x_lag] - E[x]*E[x_lag]) / (std(x) * std(x_lag))
        # Computed via rolling mean/std of products.
        abs_ret_lag = abs_ret.shift(1)
        window = 48
        min_p = 24

        mean_x = abs_ret.rolling(window, min_periods=min_p).mean()
        mean_y = abs_ret_lag.rolling(window, min_periods=min_p).mean()
        mean_xy = (abs_ret * abs_ret_lag).rolling(window, min_periods=min_p).mean()
        std_x = abs_ret.rolling(window, min_periods=min_p).std()
        std_y = abs_ret_lag.rolling(window, min_periods=min_p).std()

        cov = mean_xy - mean_x * mean_y
        denom = (std_x * std_y).replace(0, np.nan)
        return cov / denom


# ────────────────────────────────────────────────────────────
# 10. Vol-of-vol
# ────────────────────────────────────────────────────────────

@register_factor
class VolOfVol(TimeSeriesFactor):
    """Std of rolling 6h realized vol over trailing 24 bars.

    High vol-of-vol = regime is changing; low vol-of-vol = stable regime.
    Useful as a confidence modifier (weight other signals less when
    vol-of-vol is high).
    """
    name = "vol_of_vol"
    category = "volatility_structure"
    lookback_bars = 24
    description = "Std of rolling 6h realized vol over last 24h — regime stability."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        log_ret = np.log(df["close"] / df["close"].shift(1))
        rv_6h = np.sqrt((log_ret ** 2).rolling(6, min_periods=3).sum())
        return rv_6h.rolling(24, min_periods=12).std()
