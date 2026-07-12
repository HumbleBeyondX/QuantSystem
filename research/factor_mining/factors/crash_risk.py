"""Crash-risk / tail-risk factors.

Names with high crash risk underperform per the variance-risk-premium
literature. All factors sign-flipped: low crash risk = high score.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factor_mining.base import CrossSectionalFactor, register_factor
from research.factor_mining.data_panel import DataPanel
from research.factor_mining.operators import ts_max, ts_mean, ts_std


# ────────────────────────────────────────────────────────────
@register_factor
class MaxDrawdownDepth_252(CrossSectionalFactor):
    """Max drawdown over last 252 days, sign-flipped:
    shallow DD → high score."""
    name = "max_dd_depth_252"
    category = "crash_risk"
    universe_type = "multi"
    lookback_days = 252
    description = "Negative max drawdown over 252d (shallow DD = high score)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        rolling_max = ts_max(c, 252)
        dd = (c - rolling_max) / rolling_max  # ≤ 0
        # Take min (most negative) over the 252d window for each row
        worst = dd.rolling(252, min_periods=126).min()
        # Sign flip: deep negative → low score after negation
        return worst  # already negative, so "less negative" = higher = preferred


# ────────────────────────────────────────────────────────────
@register_factor
class JumpFreq_252(CrossSectionalFactor):
    """Number of "jump" days (|return| > 3σ) in last 252 days.

    More jumps = more crash risk. Sign-flipped.
    """
    name = "jump_freq_252"
    category = "crash_risk"
    universe_type = "multi"
    lookback_days = 252
    description = "Negative count of |return| > 3σ days in last 252d."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.log_returns_wide
        sigma = ts_std(ret, 252)
        jump = (ret.abs() > 3 * sigma).astype(float)
        # Sum jumps in trailing 252d
        jump_count = jump.rolling(252, min_periods=126).sum()
        return -jump_count


# ────────────────────────────────────────────────────────────
@register_factor
class TailRisk_VaR95_252(CrossSectionalFactor):
    """Empirical 5%-quantile of 252-day return distribution.

    More negative VaR = worse tail. Sign already such that "less
    negative VaR" → higher score.
    """
    name = "tail_var95_252"
    category = "crash_risk"
    universe_type = "multi"
    lookback_days = 252
    description = "5th-percentile (empirical VaR_95) of 252d returns."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.log_returns_wide
        # Rolling 5th percentile
        return ret.rolling(252, min_periods=126).quantile(0.05)


# ────────────────────────────────────────────────────────────
@register_factor
class CoVaR_proxy(CrossSectionalFactor):
    """CoVaR proxy: how much does this asset move when market is in
    its worst 5%? Lower (more negative) co-tail = riskier.

    Per Adrian-Brunnermeier 2016. Returns the asset's mean return on
    the bottom-5% market-return days.
    """
    name = "covar_proxy_252"
    category = "crash_risk"
    universe_type = "multi"
    lookback_days = 252
    description = "Asset's mean return on bottom-5% market days (252d)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.log_returns_wide
        market = ret.mean(axis=1)
        # 252-day rolling 5th percentile of market
        market_var95 = market.rolling(252, min_periods=126).quantile(0.05)
        # Boolean: is market <= var95?
        bad_market = market <= market_var95

        out = pd.DataFrame(index=ret.index, columns=ret.columns, dtype="float64")
        for col in ret.columns:
            r_bad = ret[col].where(bad_market)
            out[col] = r_bad.rolling(252, min_periods=20).mean()
        return out


# ────────────────────────────────────────────────────────────
@register_factor
class LongTailSkew_252(CrossSectionalFactor):
    """Asymmetric skew: ratio of (worst 5% mean) / (best 5% mean).

    Closer to -1 = symmetric. More negative (e.g. -2.5) = bigger
    left-tail = bad. We return the *positive* of the ratio so that
    LESS asymmetric = HIGHER score.
    """
    name = "long_tail_skew_252"
    category = "crash_risk"
    universe_type = "multi"
    lookback_days = 252
    description = "−|q5%_mean / q95%_mean| (less left-tail-heavy = high score)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        ret = panel.log_returns_wide
        n = 252
        min_p = 126

        out = pd.DataFrame(index=ret.index, columns=ret.columns, dtype="float64")
        for col in ret.columns:
            r = ret[col]
            q5 = r.rolling(n, min_periods=min_p).quantile(0.05)
            q95 = r.rolling(n, min_periods=min_p).quantile(0.95)
            ratio = (q5 / q95.replace(0, np.nan))
            # ratio is normally negative (q5 < 0 < q95). Less neg = better.
            out[col] = ratio  # higher = less left-tail
        return out
