"""Composite factors — equal-weight z-score blends of related primitives.

Compositing reduces noise versus single-factor signals at the cost of
some specificity. These three blend horizons or factor families that
academic research suggests work together.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factor_mining.base import CrossSectionalFactor, register_factor
from research.factor_mining.data_panel import DataPanel
from research.factor_mining.operators import (
    beta as op_beta,
    realized_vol,
    ts_mean,
    ts_std,
)


def _cs_zscore(x: pd.DataFrame) -> pd.DataFrame:
    mean = x.mean(axis=1)
    std = x.std(axis=1).replace(0, np.nan)
    return x.sub(mean, axis=0).div(std, axis=0)


# ────────────────────────────────────────────────────────────
@register_factor
class MultiHorizonMom(CrossSectionalFactor):
    """Equal-weight composite of mom_3_1, mom_6_1, mom_12_1 (z-scored)."""
    name = "multi_horizon_mom"
    category = "composite"
    universe_type = "multi"
    lookback_days = 252
    description = "Cross-sectional z-score average of 3m, 6m, 12m momentum."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        mom_3 = np.log(c.shift(21) / c.shift(63))
        mom_6 = np.log(c.shift(21) / c.shift(126))
        mom_12 = np.log(c.shift(21) / c.shift(252))
        return (_cs_zscore(mom_3) + _cs_zscore(mom_6) + _cs_zscore(mom_12)) / 3.0


# ────────────────────────────────────────────────────────────
@register_factor
class MultiHorizonVol(CrossSectionalFactor):
    """Equal-weight composite of −vol at 21/63/252d horizons (z-scored).
    Higher = lower-vol cross-section ranking."""
    name = "multi_horizon_vol"
    category = "composite"
    universe_type = "multi"
    lookback_days = 252
    description = "z-score average of −vol at 21d/63d/252d (low vol = high score)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.log_returns_wide
        v_21 = -realized_vol(ret, 21)
        v_63 = -realized_vol(ret, 63)
        v_252 = -realized_vol(ret, 252)
        return (_cs_zscore(v_21) + _cs_zscore(v_63) + _cs_zscore(v_252)) / 3.0


# ────────────────────────────────────────────────────────────
@register_factor
class BalancedAlpha(CrossSectionalFactor):
    """Composite of momentum × low-vol × low-beta z-scores.

    Mimics the AQR "QMJ × Mom × BAB" stack: each pillar z-scored,
    averaged equally. Avoids reliance on any single factor.
    """
    name = "balanced_alpha"
    category = "composite"
    universe_type = "multi"
    lookback_days = 252
    description = "Equal-weight z-score: momentum + low-vol + low-beta."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.log_returns_wide
        c = panel.closes_wide

        mom = np.log(c.shift(21) / c.shift(252))
        low_vol = -realized_vol(ret, 252)
        market = ret.mean(axis=1)
        betas = op_beta(ret, market, n=252)
        low_beta = -betas

        return (_cs_zscore(mom) + _cs_zscore(low_vol) + _cs_zscore(low_beta)) / 3.0
