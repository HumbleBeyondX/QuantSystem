"""Volume-based factors.

Trading volume proxies for: investor attention (high vol → news flow),
liquidity premium (low ADV stocks earn premium per Amihud), and
price-volume divergence as a return predictor.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factor_mining.base import CrossSectionalFactor, register_factor
from research.factor_mining.data_panel import DataPanel
from research.factor_mining.operators import (
    ts_corr,
    ts_mean,
    ts_sum,
    log_safe,
)


# ────────────────────────────────────────────────────────────
@register_factor
class VolumeRatio_5_to_60(CrossSectionalFactor):
    """5-day avg volume / 60-day avg volume.

    > 1 = recent surge in attention. Often a momentum-confirmation signal.
    """
    name = "volume_ratio_5_60"
    category = "volume"
    universe_type = "multi"
    lookback_days = 60
    description = "Recent 5d volume / long-term 60d volume."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        v = panel.volumes_wide
        recent = ts_mean(v, 5)
        baseline = ts_mean(v, 60).replace(0, np.nan)
        return recent / baseline


# ────────────────────────────────────────────────────────────
@register_factor
class DollarVolume_21(CrossSectionalFactor):
    """log(avg dollar volume over 21d). Higher = more liquid = lower
    illiquidity premium. Sign-flipped so low ADV → high score."""
    name = "dollar_volume_21"
    category = "volume"
    universe_type = "multi"
    lookback_days = 21
    description = "Negative log of 21-day avg dollar volume (low ADV = high score)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        dv = panel.dollar_volumes_wide
        avg_dv = ts_mean(dv, 21)
        return -log_safe(avg_dv)


# ────────────────────────────────────────────────────────────
@register_factor
class OBV_slope_21(CrossSectionalFactor):
    """On-Balance Volume slope over 21 days.

    OBV = cumulative sign(returns) × volume. A rising OBV with rising
    price = healthy uptrend; falling OBV with rising price = warning.
    Returns simple change in OBV over 21d normalized by 21d avg volume.
    """
    name = "obv_slope_21"
    category = "volume"
    universe_type = "multi"
    lookback_days = 22
    description = "21-day change in OBV, normalized by 21d avg volume."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        v = panel.volumes_wide
        ret = panel.returns_wide
        signed_v = np.sign(ret) * v
        obv = signed_v.cumsum()
        slope = obv - obv.shift(21)
        baseline = ts_mean(v, 21).replace(0, np.nan)
        return slope / baseline


# ────────────────────────────────────────────────────────────
@register_factor
class VolumePriceCorr_21(CrossSectionalFactor):
    """Rolling 21-day correlation between volume and absolute returns.

    Strong positive corr = price-volume confirmation; weak corr =
    price moves without volume backing (suspect).
    """
    name = "volume_price_corr_21"
    category = "volume"
    universe_type = "multi"
    lookback_days = 21
    description = "21-day correlation between volume and |daily returns|."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        v = panel.volumes_wide
        abs_ret = panel.returns_wide.abs()
        return ts_corr(v, abs_ret, 21)


# ────────────────────────────────────────────────────────────
@register_factor
class AmihudIlliq_21(CrossSectionalFactor):
    """Amihud (2002) illiquidity ratio:
        avg(|return| / dollar_volume) over 21 days.

    Higher = less liquid → larger illiquidity premium.
    Already in the 'good direction' (high = high score).
    """
    name = "amihud_illiq_21"
    category = "volume"
    universe_type = "multi"
    lookback_days = 21
    description = "Amihud illiquidity: avg |return|/dollar_volume."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.returns_wide.abs()
        dv = panel.dollar_volumes_wide.replace(0, np.nan)
        # log to compress the scale (raw values vary by orders of magnitude)
        ratio = ret / dv
        return ts_mean(np.log(ratio.replace(0, np.nan).fillna(np.nan) + 1e-12), 21)


# ────────────────────────────────────────────────────────────
@register_factor
class VolumeShockReversal(CrossSectionalFactor):
    """High-volume day return reverses next 5 days.

    Signal = -1 × (return on highest-volume day in last 5) × (sign of
    that volume spike). Captures the 'volume shock' anomaly.
    """
    name = "volume_shock_reversal"
    category = "volume"
    universe_type = "multi"
    lookback_days = 10
    description = "Negative of return on highest-volume day in last 5d."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        v = panel.volumes_wide
        ret = panel.returns_wide

        n = 5
        # For each (date, ticker), find which of last n days had max volume
        out = pd.DataFrame(index=v.index, columns=v.columns, dtype="float64")

        # Rolling max volume + corresponding return — vectorized via shift stacks
        vol_stack = pd.concat({i: v.shift(i) for i in range(n)}, axis=0)
        ret_stack = pd.concat({i: ret.shift(i) for i in range(n)}, axis=0)

        # For each (column, date) combo find the lag with max vol
        # Easier approach: use rolling with apply
        def _shock_ret(window: np.ndarray, ret_window: np.ndarray) -> float:
            if np.isnan(window).all() or np.isnan(ret_window).all():
                return np.nan
            valid = ~np.isnan(window) & ~np.isnan(ret_window)
            if valid.sum() < 2:
                return np.nan
            v = window[valid]
            r = ret_window[valid]
            idx = np.argmax(v)
            return -r[idx]

        for col in v.columns:
            v_arr = v[col].values
            r_arr = ret[col].values
            outs = np.full(len(v_arr), np.nan)
            for i in range(n - 1, len(v_arr)):
                outs[i] = _shock_ret(v_arr[i - n + 1: i + 1], r_arr[i - n + 1: i + 1])
            out[col] = outs
        return out
