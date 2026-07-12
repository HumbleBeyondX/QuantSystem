"""Microstructure / intraday OHLC pattern factors.

Without true tick data, we extract microstructure-flavored signals
from the daily OHLC: high-low range, close-to-open gap, body/wick
ratios. These approximate intraday volatility, overnight risk, and
candle-conviction information.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factor_mining.base import CrossSectionalFactor, register_factor
from research.factor_mining.data_panel import DataPanel
from research.factor_mining.operators import ts_mean


# ────────────────────────────────────────────────────────────
@register_factor
class HighLowRange_21(CrossSectionalFactor):
    """Parkinson (1980) volatility estimator over last 21 days.

        σ_P² ≈ 1/(4·ln 2) · mean((ln(H/L))²)

    More efficient than close-to-close vol. Sign-flipped: low Parkinson
    σ → high score (low-vol preference).
    """
    name = "parkinson_vol_21"
    category = "microstructure"
    universe_type = "multi"
    lookback_days = 21
    description = "Parkinson high-low volatility estimator (sign-flipped)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        h = panel.highs_wide
        l = panel.lows_wide
        log_hl_sq = (np.log(h / l)) ** 2
        scale = 1.0 / (4 * np.log(2))
        var = ts_mean(log_hl_sq, 21) * scale
        return -np.sqrt(var * 252)


# ────────────────────────────────────────────────────────────
@register_factor
class GarmanKlass_21(CrossSectionalFactor):
    """Garman-Klass (1980) volatility estimator over 21 days.

        σ_GK² = 0.5·(ln(H/L))² − (2 ln 2 − 1)·(ln(C/O))²

    Sign-flipped (low-vol preference).
    """
    name = "garman_klass_21"
    category = "microstructure"
    universe_type = "multi"
    lookback_days = 21
    description = "Garman-Klass volatility estimator (sign-flipped)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        h = panel.highs_wide
        l = panel.lows_wide
        c = panel.closes_wide
        o = panel.opens_wide
        term1 = 0.5 * (np.log(h / l)) ** 2
        term2 = (2 * np.log(2) - 1) * (np.log(c / o)) ** 2
        var_d = ts_mean(term1 - term2, 21).clip(lower=0)
        return -np.sqrt(var_d * 252)


# ────────────────────────────────────────────────────────────
@register_factor
class CloseToOpenGap_21(CrossSectionalFactor):
    """Avg overnight gap return over 21 days: log(open[t] / close[t-1]).

    Persistent positive gaps → buyers waking up positively → mom signal.
    """
    name = "overnight_gap_21"
    category = "microstructure"
    universe_type = "multi"
    lookback_days = 22
    description = "21-day mean overnight gap log(open[t]/close[t-1])."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        o = panel.opens_wide
        c = panel.closes_wide
        gap = np.log(o / c.shift(1))
        return ts_mean(gap, 21)


# ────────────────────────────────────────────────────────────
@register_factor
class IntradayMom_21(CrossSectionalFactor):
    """Avg of (close − open) / (high − low) over 21 days.

    Captures intraday "where in the range did we close?" — high values
    = buyers in control day after day.
    """
    name = "intraday_mom_21"
    category = "microstructure"
    universe_type = "multi"
    lookback_days = 21
    description = "21-day mean of (close-open)/(high-low)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        h = panel.highs_wide
        l = panel.lows_wide
        c = panel.closes_wide
        o = panel.opens_wide
        rng = (h - l).replace(0, np.nan)
        return ts_mean((c - o) / rng, 21)


# ────────────────────────────────────────────────────────────
@register_factor
class BodyToWick_21(CrossSectionalFactor):
    """Avg candle body / total range over 21 days.

    body = |close - open|, range = high - low.
    High = strong-conviction trending bars; low = indecision.
    """
    name = "body_to_wick_21"
    category = "microstructure"
    universe_type = "multi"
    lookback_days = 21
    description = "Avg |close-open|/(high-low) over 21 days, signed by direction."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        h = panel.highs_wide
        l = panel.lows_wide
        c = panel.closes_wide
        o = panel.opens_wide
        body = (c - o).abs()
        rng = (h - l).replace(0, np.nan)
        signed = np.sign(c - o) * body / rng
        return ts_mean(signed, 21)
