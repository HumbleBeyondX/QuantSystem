"""Derivatives factors — funding rate and open-interest signals.

Requires optional columns on the input DataFrame:
  * funding_rate (per-8h OKX perpetual funding rate)
  * open_interest (USDT-notional open interest)

When these columns are absent the factors return all-NaN (evaluator drops
them). The data fetcher script populates them from OKX public endpoints.

Sign convention:
  * Funding: negative z-score (shorts paying longs) is bullish for ETH.
    We return the NEGATIVE of the z-score so positive values are bullish.
  * OI-price divergence: positive means OI rising with price (persistence
    signal, bullish continuation).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factors.ts_factor_base import TimeSeriesFactor, register_factor


def _has_funding(df: pd.DataFrame) -> bool:
    return "funding_rate" in df.columns and df["funding_rate"].notna().any()


def _has_oi(df: pd.DataFrame) -> bool:
    return "open_interest" in df.columns and df["open_interest"].notna().any()


# ────────────────────────────────────────────────────────────
# 18. Funding rate z-score (sign-flipped)
# ────────────────────────────────────────────────────────────

@register_factor
class FundingRateZScore(TimeSeriesFactor):
    """-z_score(funding, window=30 days).

    OKX funding is charged every 8h. Large positive funding means longs
    are paying shorts (crowded long → bearish contrarian signal). We flip
    the sign so positive factor values are bullish.
    """
    name = "funding_rate_zscore"
    category = "derivatives"
    lookback_bars = 24 * 30
    description = "Negative z-score of funding rate over 30d — contrarian crowd indicator."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        if not _has_funding(df):
            return pd.Series(np.nan, index=df.index)
        fr = df["funding_rate"]
        mean = fr.rolling(24 * 30, min_periods=24 * 7).mean()
        std = fr.rolling(24 * 30, min_periods=24 * 7).std().replace(0, np.nan)
        return -(fr - mean) / std


# ────────────────────────────────────────────────────────────
# 19. Funding rate trend
# ────────────────────────────────────────────────────────────

@register_factor
class FundingRateTrend(TimeSeriesFactor):
    """Slope of funding rate over trailing 24h.

    Funding trending up → long-side getting more crowded → bearish.
    Returns sign-flipped slope so positive = bullish per convention.
    """
    name = "funding_rate_trend_24h"
    category = "derivatives"
    lookback_bars = 24
    description = "Negative 24h OLS slope of funding rate — crowd-building direction."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        if not _has_funding(df):
            return pd.Series(np.nan, index=df.index)

        # Vectorized rolling OLS slope: cov(t, y) / var(t) where t is the
        # position index within each window. For a window of length w, var(t)
        # is constant so we only need rolling cov.
        window = 24
        min_p = 12
        fr = df["funding_rate"]

        # Center position: rolling corr of (funding, t_in_window)
        t_idx = pd.Series(np.arange(len(fr), dtype=np.float64), index=fr.index)
        mean_x = fr.rolling(window, min_periods=min_p).mean()
        mean_t = t_idx.rolling(window, min_periods=min_p).mean()
        mean_xt = (fr * t_idx).rolling(window, min_periods=min_p).mean()
        var_t = t_idx.rolling(window, min_periods=min_p).var().replace(0, np.nan)

        cov_xt = mean_xt - mean_x * mean_t
        slope = cov_xt / var_t
        return -slope


# ────────────────────────────────────────────────────────────
# 20. OI-price divergence
# ────────────────────────────────────────────────────────────

@register_factor
class OIPriceDivergence(TimeSeriesFactor):
    """Signed match between OI change and price change over trailing 24h.

      * +1: both OI and price rising (bullish continuation — real money in)
      * -1: price rising but OI falling (short covering rally — weak)
    """
    name = "oi_price_divergence"
    category = "derivatives"
    lookback_bars = 24
    description = "Sign match between 24h change in OI and 24h change in price."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        if not _has_oi(df):
            return pd.Series(np.nan, index=df.index)
        price_chg = df["close"] / df["close"].shift(24) - 1.0
        oi_chg = df["open_interest"] / df["open_interest"].shift(24) - 1.0
        price_sign = np.sign(price_chg)
        oi_sign = np.sign(oi_chg)
        return price_sign * oi_sign
