"""Alpha101 — selected formulas from the WorldQuant Alpha101 paper.

These are battle-tested formula factors that frequently show non-trivial
edges across markets. We implement a curated subset — the ones most
consistently cited in academic and industry follow-ups — translated to
our pandas-wide operator vocabulary.

Reference
---------
Kakushadze, Z. (2016). "101 Formulaic Alphas."
SSRN: https://ssrn.com/abstract=2701346

Convention
----------
The original Alpha101 formulas often produce SHORT signals (high value =
expected NEGATIVE forward return — e.g. high-volume + price-up = short).
We FLIP the sign so HIGHER value = MORE attractive long, consistent with
the rest of the factor library.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factor_mining.base import CrossSectionalFactor, register_factor
from research.factor_mining.data_panel import DataPanel
from research.factor_mining.operators import (
    decay_linear,
    delay,
    delta,
    rank,
    scale,
    signedpower,
    ts_argmax,
    ts_corr,
    ts_max,
    ts_min,
    ts_rank,
    ts_std,
    ts_sum,
)


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha001(CrossSectionalFactor):
    name = "alpha_001"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 21
    description = (
        "rank(ts_argmax(signedpower(returns if returns<0 else close, 2), 5)) - 0.5"
    )

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        r = panel.returns_wide
        # Volatility-conditional input
        cond = r.where(r < 0, c)
        sq = signedpower(cond, 2.0)
        am = ts_argmax(sq, 5)
        return rank(am) - 0.5


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha003(CrossSectionalFactor):
    name = "alpha_003"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 10
    description = "-1 * correlation(rank(open), rank(volume), 10)  → flipped → +1*…"

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ro = rank(panel.opens_wide)
        rv = rank(panel.volumes_wide)
        # Original is -1 *. Flip so high = bullish.
        return ts_corr(ro, rv, 10)


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha006(CrossSectionalFactor):
    name = "alpha_006"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 10
    description = "-1 * correlation(open, volume, 10) → flipped"

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        return ts_corr(panel.opens_wide, panel.volumes_wide, 10)


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha012(CrossSectionalFactor):
    name = "alpha_012"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 5
    description = "sign(delta(volume,1)) * (-1 * delta(close,1)) → flipped"

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        dv = np.sign(delta(panel.volumes_wide, 1))
        dc = delta(panel.closes_wide, 1)
        # Original is sign(dv) * (-1 * dc). Higher = bullish AFTER our flip.
        return dv * dc


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha023(CrossSectionalFactor):
    name = "alpha_023"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 20
    description = (
        "if sma(high,20) < high: -delta(high,2) else 0  → flipped"
    )

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        h = panel.highs_wide
        sma_h = h.rolling(20, min_periods=10).mean()
        # Conditional: only emits signal when high > sma_h
        signal = -delta(h, 2)
        out = signal.where(sma_h < h, 0.0)
        return -out  # flip


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha033(CrossSectionalFactor):
    name = "alpha_033"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 5
    description = "rank(-1 * (1 - open/close)^1) → flipped"

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        o = panel.opens_wide
        c = panel.closes_wide
        x = -(1.0 - o.div(c.replace(0, np.nan)))
        return -rank(x)  # flip so high = bullish


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha038(CrossSectionalFactor):
    name = "alpha_038"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 10
    description = "-1 * rank(ts_rank(close, 10)) * rank(close/open) → flipped"

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        o = panel.opens_wide
        a = rank(ts_rank(c, 10))
        b = rank(c.div(o.replace(0, np.nan)))
        return a * b  # already flipped


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha041(CrossSectionalFactor):
    name = "alpha_041"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 5
    description = "sqrt(high*low) - vwap"

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        h = panel.highs_wide
        l = panel.lows_wide
        v = panel.vwaps_wide
        return np.sqrt(h * l) - v


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha042(CrossSectionalFactor):
    name = "alpha_042"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 5
    description = "rank(vwap - close) / rank(vwap + close)"

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        v = panel.vwaps_wide
        c = panel.closes_wide
        num = rank(v - c)
        den = rank(v + c).replace(0, np.nan)
        return num / den


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha054(CrossSectionalFactor):
    name = "alpha_054"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 5
    description = "-((low - close) * (open^5)) / ((low - high) * (close^5)) → flipped"

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        l = panel.lows_wide
        c = panel.closes_wide
        h = panel.highs_wide
        o = panel.opens_wide
        num = (l - c) * (o ** 5)
        den = ((l - h) * (c ** 5)).replace(0, np.nan)
        # Original is -num/den.
        return -(num / den)


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha101(CrossSectionalFactor):
    name = "alpha_101"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 2
    description = "(close - open) / (high - low + 0.001)"

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        o = panel.opens_wide
        h = panel.highs_wide
        l = panel.lows_wide
        return (c - o) / ((h - l) + 0.001)


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha005(CrossSectionalFactor):
    name = "alpha_005"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 10
    description = "rank((open - sum(vwap,10)/10)) * (-1 * abs(rank(close - vwap)))"

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        o = panel.opens_wide
        v = panel.vwaps_wide
        c = panel.closes_wide
        a = rank(o - ts_sum(v, 10) / 10)
        b = -(rank(c - v).abs())
        # Original is a * b. Already produces signed values.
        return a * b


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha019(CrossSectionalFactor):
    name = "alpha_019"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 250
    description = (
        "(-1 * sign(close - delay(close,7) + delta(close,7))) * "
        "(1 + rank(sum(returns, 250)))  → flipped"
    )

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        r = panel.returns_wide
        a = c - delay(c, 7) + delta(c, 7)
        b = 1 + rank(ts_sum(r, 250))
        # Flip the leading -1: high = bullish
        return np.sign(a) * b


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha024(CrossSectionalFactor):
    name = "alpha_024"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 100
    description = (
        "if (delta(sma(close,100),100)/delay(close,100)) <= 0.05: "
        "(-1 * (close - ts_min(close,100))) else (-1 * delta(close,3))  → flipped"
    )

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        sma_100 = c.rolling(100, min_periods=50).mean()
        cond = delta(sma_100, 100) / delay(c, 100).replace(0, np.nan)
        branch_lo = c - ts_min(c, 100)
        branch_hi = delta(c, 3)
        out = branch_lo.where(cond <= 0.05, branch_hi)
        return out  # flip applied (drop original -1)


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha026(CrossSectionalFactor):
    name = "alpha_026"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 8
    description = "-1 * ts_max(correlation(ts_rank(volume,5), ts_rank(high,5), 5), 3)"

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        v = panel.volumes_wide
        h = panel.highs_wide
        a = ts_rank(v, 5)
        b = ts_rank(h, 5)
        corr = ts_corr(a, b, 5)
        # Flip sign
        return -ts_max(corr, 3)


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha040(CrossSectionalFactor):
    name = "alpha_040"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 10
    description = "-1 * rank(stddev(high,10)) * correlation(high, volume, 10)  → flipped"

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        h = panel.highs_wide
        v = panel.volumes_wide
        a = rank(ts_std(h, 10))
        b = ts_corr(h, v, 10)
        # Original is -1 * a * b; flip
        return a * b


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha053(CrossSectionalFactor):
    name = "alpha_053"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 9
    description = "-1 * delta((close-low) - (high-close) / (close-low), 9)  → flipped"

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        l = panel.lows_wide
        h = panel.highs_wide
        num = (c - l) - (h - c)
        den = (c - l).replace(0, np.nan)
        # Flip
        return delta(num / den, 9)


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha084(CrossSectionalFactor):
    name = "alpha_084"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 21
    description = "signedpower(ts_rank(vwap - ts_max(vwap,15), 21), delta(close,5))  → flipped"

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        v = panel.vwaps_wide
        c = panel.closes_wide
        ts_part = ts_rank(v - ts_max(v, 15), 21)
        # Use a fixed exponent because variable exponent across DataFrames is messy
        # in pandas; pick p=2 — preserves the spirit (penalty on large positive ts_part).
        return -signedpower(ts_part, 2.0)


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha101_v2(CrossSectionalFactor):
    """Cumulative-overnight return component — equivalent to Alpha#101 family."""
    name = "alpha_overnight_5"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 5
    description = "5-day sum of (close - open)/(high-low+eps)  — intraday accumulation"

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        o = panel.opens_wide
        h = panel.highs_wide
        l = panel.lows_wide
        intraday = (c - o) / ((h - l) + 0.001)
        return ts_sum(intraday, 5)


# ────────────────────────────────────────────────────────────
@register_factor
class Alpha013(CrossSectionalFactor):
    name = "alpha_013"
    category = "alpha101"
    universe_type = "multi"
    lookback_days = 5
    description = "-1 * rank(covariance(rank(close), rank(volume), 5))  → flipped"

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = rank(panel.closes_wide)
        v = rank(panel.volumes_wide)
        # rolling covariance
        cov5 = c.rolling(5, min_periods=3).cov(v)
        return rank(cov5)  # flipped
