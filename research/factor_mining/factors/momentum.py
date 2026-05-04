"""Momentum factors — academic standards.

All factors return wide DataFrames (date × ticker). HIGHER value =
expected positive forward return.

Family
------
1. Mom_12_1   — Carhart (1997) momentum (12m return ex last month)
2. Mom_6_1    — 6-month momentum, skip last month
3. Mom_3_1    — 3-month momentum, skip last month
4. Mom_1m     — 1-month momentum (raw, no skip — used as control)
5. ResidualMomentum_252 — alpha after market regression, last 12m
6. TimeSeriesMom_12   — Moskowitz et al (2012): sign of own 12m return
7. PureTrend          — Mom_6_1 minus STR_1m (trend net of reversal)
8. RelativeStrength_252 — close / max(close, 252)  (proximity to 1y high)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factor_mining.base import CrossSectionalFactor, register_factor
from research.factor_mining.data_panel import DataPanel
from research.factor_mining.operators import ts_max


# ────────────────────────────────────────────────────────────
@register_factor
class Mom_12_1(CrossSectionalFactor):
    name = "mom_12_1"
    category = "momentum"
    universe_type = "multi"
    lookback_days = 252
    description = "Carhart 12-month momentum, skipping the most recent month."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        # log(c[t-21] / c[t-252])  — last month skipped
        return np.log(c.shift(21) / c.shift(252))


# ────────────────────────────────────────────────────────────
@register_factor
class Mom_6_1(CrossSectionalFactor):
    name = "mom_6_1"
    category = "momentum"
    universe_type = "multi"
    lookback_days = 126
    description = "6-month momentum, skipping the most recent month."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        return np.log(c.shift(21) / c.shift(126))


# ────────────────────────────────────────────────────────────
@register_factor
class Mom_3_1(CrossSectionalFactor):
    name = "mom_3_1"
    category = "momentum"
    universe_type = "multi"
    lookback_days = 63
    description = "3-month momentum, skipping the most recent month."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        return np.log(c.shift(21) / c.shift(63))


# ────────────────────────────────────────────────────────────
@register_factor
class Mom_1m(CrossSectionalFactor):
    name = "mom_1m_raw"
    category = "momentum"
    universe_type = "multi"
    lookback_days = 21
    description = "Raw 1-month momentum (often anti-momentum / reversal)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        return np.log(c / c.shift(21))


# ────────────────────────────────────────────────────────────
@register_factor
class ResidualMomentum_252(CrossSectionalFactor):
    """Residual momentum: alpha after regressing each ticker's returns
    on the cross-sectional mean (proxy for 'market'). Captures the
    idiosyncratic component of momentum, less prone to crashes."""
    name = "resid_mom_252"
    category = "momentum"
    universe_type = "multi"
    lookback_days = 252
    description = "Residual 12-month momentum after market regression."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        log_ret = panel.log_returns_wide
        market = log_ret.mean(axis=1)  # equal-weighted "market"
        # Rolling 252-day OLS residual mean per ticker
        out = pd.DataFrame(index=log_ret.index, columns=log_ret.columns, dtype="float64")
        n = 252
        for col in log_ret.columns:
            r = log_ret[col]
            cov = r.rolling(n, min_periods=126).cov(market)
            var = market.rolling(n, min_periods=126).var().replace(0, np.nan)
            beta = cov / var
            alpha = r.rolling(n, min_periods=126).mean() - beta * market.rolling(n, min_periods=126).mean()
            # Residual return over last 12-1 month: use the alpha as factor
            out[col] = alpha * 252  # annualize
        return out


# ────────────────────────────────────────────────────────────
@register_factor
class TimeSeriesMom_12(CrossSectionalFactor):
    """Moskowitz/Ooi/Pedersen 2012 — time-series momentum.
    Each ticker independent: long if 12m return > 0, short if < 0.
    Cross-sectional value = the actual 12m return (sign carries info)."""
    name = "ts_mom_12"
    category = "momentum"
    universe_type = "multi"
    lookback_days = 252
    description = "Time-series momentum: own 12-month return."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        return np.log(c / c.shift(252))


# ────────────────────────────────────────────────────────────
@register_factor
class PureTrend(CrossSectionalFactor):
    """Mom_6_1 minus STR_1m — trend signal net of short-term reversal.
    Reduces 'momentum crash' risk during reversals."""
    name = "pure_trend"
    category = "momentum"
    universe_type = "multi"
    lookback_days = 126
    description = "6-month momentum minus 1-month return (trend net of reversal)."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        mom = np.log(c.shift(21) / c.shift(126))
        str1m = np.log(c / c.shift(21))
        return mom - str1m


# ────────────────────────────────────────────────────────────
@register_factor
class RelativeStrength_252(CrossSectionalFactor):
    """Close / max(close, 252) — proximity to 1-year high.
    Values close to 1 = near the high (positive momentum proxy).
    """
    name = "rel_strength_252"
    category = "momentum"
    universe_type = "multi"
    lookback_days = 252
    description = "Current price / 252-day rolling high."

    def compute(self, panel: DataPanel) -> pd.DataFrame:
        c = panel.closes_wide
        rolling_max = ts_max(c, 252)
        return c / rolling_max
