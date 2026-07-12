"""Microstructure factors — candle-level price/volume geometry.

These factors look at the shape of individual candles and how price moves
relative to volume. They're mostly unrelated to the EMA/RSI/MACD family and
capture intra-bar order flow information that longer-lookback indicators miss.

Sign convention:
  * Positive = bullish (expect positive forward return)
  * Negative = bearish
  * NaN when data is insufficient or degenerate (zero-range bar, etc.)
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factors.ts_factor_base import TimeSeriesFactor, register_factor


# ────────────────────────────────────────────────────────────
# 1. Candle body vs. full range — indecision detector
# ────────────────────────────────────────────────────────────

@register_factor
class BodyToRangeRatio(TimeSeriesFactor):
    """|close-open| / (high-low), signed by close direction.

    Interpretation:
      * |value| near 1.0 → strong conviction move (full-body candle).
      * |value| near 0  → indecision / wick-heavy bar (doji).
      * Sign follows the close direction (positive if close > open).

    Empirical hypothesis: strong-body bars persist in direction; doji bars
    often mean reversion.
    """
    name = "body_to_range_ratio"
    category = "microstructure"
    lookback_bars = 1
    description = "Signed candle body as fraction of full range — conviction/indecision proxy."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        direction = np.sign(df["close"] - df["open"])
        body = (df["close"] - df["open"]).abs()
        rng = (df["high"] - df["low"]).replace(0, np.nan)
        return direction * body / rng


# ────────────────────────────────────────────────────────────
# 2. Upper wick pressure — rejection at highs
# ────────────────────────────────────────────────────────────

@register_factor
class UpperWickPressure(TimeSeriesFactor):
    """(high - max(open,close)) / (high-low).

    Large value → strong rejection at the high, *bearish*. Sign is flipped
    (multiplied by -1) so positive values mean bullish per convention.
    """
    name = "upper_wick_pressure"
    category = "microstructure"
    lookback_bars = 1
    description = "Upper shadow relative to range — negative of rejection-at-high pressure."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        body_top = df[["open", "close"]].max(axis=1)
        upper_wick = df["high"] - body_top
        rng = (df["high"] - df["low"]).replace(0, np.nan)
        # Flip sign so "big upper wick" → negative (bearish)
        return -(upper_wick / rng)


# ────────────────────────────────────────────────────────────
# 3. Lower wick pressure — rejection at lows
# ────────────────────────────────────────────────────────────

@register_factor
class LowerWickPressure(TimeSeriesFactor):
    """(min(open,close) - low) / (high-low).

    Large value → strong rejection at the low, *bullish*. Already aligned
    so no sign flip needed.
    """
    name = "lower_wick_pressure"
    category = "microstructure"
    lookback_bars = 1
    description = "Lower shadow relative to range — rejection-at-low (bullish) pressure."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        body_bottom = df[["open", "close"]].min(axis=1)
        lower_wick = body_bottom - df["low"]
        rng = (df["high"] - df["low"]).replace(0, np.nan)
        return lower_wick / rng


# ────────────────────────────────────────────────────────────
# 4. Volume-weighted price deviation
# ────────────────────────────────────────────────────────────

@register_factor
class VWAPDeviation24h(TimeSeriesFactor):
    """(close - VWAP_24h) / ATR_24h.

    Rolling 24-bar VWAP approximates the "fair value" over the day; the
    z-scored deviation indicates overbought (+) or oversold (-) vs volume-
    weighted expectation.
    """
    name = "vwap_dev_24h"
    category = "microstructure"
    lookback_bars = 24
    description = "Close deviation from 24h VWAP, normalized by 24h ATR."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        # Rolling VWAP — sum(price*volume) / sum(volume) over 24 bars
        typical = (df["high"] + df["low"] + df["close"]) / 3
        pv = typical * df["volume"]
        vwap = pv.rolling(24, min_periods=12).sum() / df["volume"].rolling(24, min_periods=12).sum()

        # Simple ATR proxy: rolling range mean
        rng = df["high"] - df["low"]
        atr_24 = rng.rolling(24, min_periods=12).mean().replace(0, np.nan)

        return (df["close"] - vwap) / atr_24
