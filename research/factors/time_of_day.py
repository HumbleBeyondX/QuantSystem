"""Time-of-day and calendar factors — session effects and seasonality.

Crypto trades 24/7, but human participation is not uniform. Asian-session
(00:00-08:00 UTC) behavior often differs from US-session (13:00-21:00 UTC),
and weekend vs weekday flows have distinct characteristics.

Sign convention: positive = bullish expectation, per ts_factor_base.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factors.ts_factor_base import TimeSeriesFactor, register_factor


# ────────────────────────────────────────────────────────────
# 5. Asian-session trend persistence
# ────────────────────────────────────────────────────────────

@register_factor
class AsianSessionTrend(TimeSeriesFactor):
    """Mean log-return during Asian session (00-08 UTC) over last 5 days.

    Forward-looking hypothesis: a consistent Asian-session bias (e.g. 5-day
    average clearly positive) tends to persist into the following
    European/US open.
    """
    name = "asian_session_trend_5d"
    category = "time_of_day"
    lookback_bars = 24 * 5
    description = "Mean 1H log-return during Asian session (00-08 UTC), last 5 days."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        hours = df["timestamp"].dt.hour
        in_asian = (hours >= 0) & (hours < 8)
        log_ret = np.log(df["close"] / df["close"].shift(1))

        masked = log_ret.where(in_asian, np.nan)
        return masked.rolling(24 * 5, min_periods=12).mean()


# ────────────────────────────────────────────────────────────
# 6. US-session volume surge
# ────────────────────────────────────────────────────────────

@register_factor
class USSessionVolumeSurge(TimeSeriesFactor):
    """Current volume / avg US-session volume over last 30 days.

    Large value during the US session (13-21 UTC) signals unusual
    institutional flow; smaller elsewhere. Sign follows close direction.
    """
    name = "us_session_volume_surge"
    category = "time_of_day"
    lookback_bars = 24 * 30
    description = "Volume ratio vs 30-day US-session average, signed by close direction."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        hours = df["timestamp"].dt.hour
        in_us = (hours >= 13) & (hours < 21)

        us_volume = df["volume"].where(in_us, np.nan)
        us_vol_mean = us_volume.rolling(24 * 30, min_periods=24 * 7).mean()

        ratio = df["volume"] / us_vol_mean
        direction = np.sign(df["close"] - df["open"])
        return (direction * ratio).where(in_us, np.nan)


# ────────────────────────────────────────────────────────────
# 7. Weekend return continuation
# ────────────────────────────────────────────────────────────

@register_factor
class WeekendContinuation(TimeSeriesFactor):
    """Signed weekend return propagated to Monday open.

    Crypto weekend flows (Sat-Sun UTC) are often lighter volume but more
    retail-driven; large weekend moves sometimes continue, sometimes
    reverse. This factor captures the former hypothesis for the first
    few hours of Monday. Zero on non-Monday-morning bars.
    """
    name = "weekend_continuation"
    category = "time_of_day"
    lookback_bars = 48
    description = "Signed 48h weekend return, attributed to Mon 00-08 UTC bars."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        ts = df["timestamp"]
        is_monday_am = (ts.dt.dayofweek == 0) & (ts.dt.hour < 8)
        # Return from 48h ago (Sat 00 UTC roughly)
        ret_48h = df["close"] / df["close"].shift(48) - 1.0
        return ret_48h.where(is_monday_am, 0.0)
