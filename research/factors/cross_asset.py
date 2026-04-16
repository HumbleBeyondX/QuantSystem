"""Cross-asset factors — ETH behavior relative to BTC.

These factors require a BTC close price column ('btc_close') to be attached
to the input DataFrame. The factor evaluator script joins BTC data in
before calling compute(). If 'btc_close' is missing the factor returns
all-NaN (factor evaluator will drop it from the results table).

Sign convention: positive = bullish for ETH.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.factors.ts_factor_base import TimeSeriesFactor, register_factor


def _has_btc(df: pd.DataFrame) -> bool:
    return "btc_close" in df.columns and df["btc_close"].notna().any()


# ────────────────────────────────────────────────────────────
# 15. BTC-ETH spread z-score
# ────────────────────────────────────────────────────────────

@register_factor
class BtcEthSpreadZScore(TimeSeriesFactor):
    """(ETH_ret - BTC_ret) z-scored over rolling 72h.

    Large positive → ETH outperforming BTC abnormally (potential mean
    reversion OR regime shift). In practice: positive z-score with
    confirming volume tends to persist; without volume, reverts.

    Strategies use this combined with volume / momentum.
    """
    name = "btc_eth_spread_zscore_72h"
    category = "cross_asset"
    lookback_bars = 72
    description = "Z-score of (ETH_ret - BTC_ret) over last 72 bars."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        if not _has_btc(df):
            return pd.Series(np.nan, index=df.index)
        eth_ret = np.log(df["close"] / df["close"].shift(1))
        btc_ret = np.log(df["btc_close"] / df["btc_close"].shift(1))
        spread = eth_ret - btc_ret
        mean = spread.rolling(72, min_periods=36).mean()
        std = spread.rolling(72, min_periods=36).std().replace(0, np.nan)
        return (spread - mean) / std


# ────────────────────────────────────────────────────────────
# 16. BTC-ETH correlation
# ────────────────────────────────────────────────────────────

@register_factor
class BtcEthCorrelation24h(TimeSeriesFactor):
    """Rolling 24h Pearson correlation between ETH and BTC log-returns.

    Low/negative correlation = ETH is decoupled from BTC (idiosyncratic
    regime). High = ETH is BTC-follower. Raw magnitude signal; strategies
    interpret separately.
    """
    name = "btc_eth_correlation_24h"
    category = "cross_asset"
    lookback_bars = 24
    description = "Rolling 24h correlation of ETH vs BTC log-returns."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        if not _has_btc(df):
            return pd.Series(np.nan, index=df.index)
        eth_ret = np.log(df["close"] / df["close"].shift(1))
        btc_ret = np.log(df["btc_close"] / df["btc_close"].shift(1))
        return eth_ret.rolling(24, min_periods=12).corr(btc_ret)


# ────────────────────────────────────────────────────────────
# 17. ETH beta vs BTC (168h = 1 week)
# ────────────────────────────────────────────────────────────

@register_factor
class EthBetaVsBtc168h(TimeSeriesFactor):
    """Rolling 168h beta of ETH returns on BTC returns.

    Beta > 1 → ETH amplifies BTC moves (bullish if BTC trending up).
    Beta < 1 → ETH more defensive. Combine with BTC trend direction.
    """
    name = "eth_beta_vs_btc_168h"
    category = "cross_asset"
    lookback_bars = 168
    description = "Rolling 1-week beta of ETH on BTC."

    def compute(self, df: pd.DataFrame) -> pd.Series:
        if not _has_btc(df):
            return pd.Series(np.nan, index=df.index)
        eth_ret = np.log(df["close"] / df["close"].shift(1))
        btc_ret = np.log(df["btc_close"] / df["btc_close"].shift(1))
        cov = eth_ret.rolling(168, min_periods=84).cov(btc_ret)
        var = btc_ret.rolling(168, min_periods=84).var().replace(0, np.nan)
        return cov / var
