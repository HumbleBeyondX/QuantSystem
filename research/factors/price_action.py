"""Price-action factors — geometric patterns, acceleration, and compression.

These look at how returns evolve across multiple bars, capturing patterns
that indicator-based factors miss.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factors.ts_factor_base import TimeSeriesFactor, register_factor


# ────────────────────────────────────────────────────────────
# 11. Momentum acceleration (second derivative of log price)
# ────────────────────────────────────────────────────────────

@register_factor
class MomentumAcceleration6h(TimeSeriesFactor):
    """d(return)/dt over 6h windows — discrete second derivative of log price.

    Bullish when recent 6h return > prior 6h return (trend accelerating up).
    Bearish when the opposite.
    """
    name = "momentum_accel_6h"
    category = "price_action"
    lookback_bars = 12
    description = "Difference between last-6h and prior-6h log returns."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        log_price = np.log(df["close"])
        recent = log_price - log_price.shift(6)
        prior = log_price.shift(6) - log_price.shift(12)
        return recent - prior


# ────────────────────────────────────────────────────────────
# 12. Range contraction (squeeze detector)
# ────────────────────────────────────────────────────────────

@register_factor
class RangeContraction(TimeSeriesFactor):
    """(last 24h range) / (prior 48h range).

    Value << 1 → range is contracting (volatility squeeze), often precedes
    an expansion breakout. Value > 1 → expanding range.

    Raw magnitude; strategies should combine with direction signals.
    """
    name = "range_contraction"
    category = "price_action"
    lookback_bars = 72
    description = "Current 24h range vs prior 48h range — volatility squeeze/expansion."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        rng = df["high"] - df["low"]
        recent = rng.rolling(24, min_periods=12).mean()
        prior = rng.shift(24).rolling(48, min_periods=24).mean().replace(0, np.nan)
        # Return log ratio so values cluster around 0 (not 1)
        return np.log(recent / prior)


# ────────────────────────────────────────────────────────────
# 13. Fractal break — 5-bar fractal high/low breakout
# ────────────────────────────────────────────────────────────

@register_factor
class FractalBreak5(TimeSeriesFactor):
    """+1 if close > rolling 5-bar fractal high, -1 if < fractal low, else 0.

    A "fractal high" is the highest high over the past 5 bars (shifted by
    1). Breaking it signals short-term trend continuation.
    """
    name = "fractal_break_5"
    category = "price_action"
    lookback_bars = 6
    description = "Discrete {-1, 0, +1} breakout indicator over 5-bar fractal extremes."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        hi5 = df["high"].shift(1).rolling(5, min_periods=5).max()
        lo5 = df["low"].shift(1).rolling(5, min_periods=5).min()
        up = df["close"] > hi5
        down = df["close"] < lo5
        return pd.Series(
            np.where(up, 1.0, np.where(down, -1.0, 0.0)),
            index=df.index,
        )


# ────────────────────────────────────────────────────────────
# 14. Inside-bar streak
# ────────────────────────────────────────────────────────────

@register_factor
class InsideBarStreak(TimeSeriesFactor):
    """Count of consecutive inside-bars (high<prev_high & low>prev_low).

    Inside bars are consolidation; long streaks precede breakouts. Factor
    value is negative of the streak count (so a long streak gives a
    negative value — "coiled spring" expected to resolve either way).

    Sign alone is ambiguous; combine with another directional factor.
    """
    name = "inside_bar_streak"
    category = "price_action"
    lookback_bars = 10
    description = "Negative count of consecutive inside-bars — coiled-spring signal."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        is_inside = (df["high"] < df["high"].shift(1)) & (df["low"] > df["low"].shift(1))

        # Vectorized streak length — groupby cumcount trick:
        #   each "False" in is_inside resets the group counter.
        arr = is_inside.fillna(False).astype(int).values
        # Group id increments on each False (non-inside bar)
        group_id = (arr == 0).cumsum()
        streak = pd.Series(arr, index=df.index).groupby(group_id).cumsum()
        return pd.Series(-streak.values, index=df.index, dtype="float64")
