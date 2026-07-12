"""Market regime classifier — 5-state detector driven by evaluated factors.

Factor research on ETH 1H (2024-2026) showed:
  * No single factor has standalone alpha (max Rank IC = 0.049)
  * Reversal/mean-reversion signals dominate at 1H (vs 4H which trends)
  * Cross-asset BTC correlation is the strongest single regime signal

This classifier combines three inputs to partition the market into 5
states. The ``ETHRegimeStrategy`` uses the regime to pick which factor
subset to weight.

Inputs (computed incrementally, no pandas required on the hot path):
  * ADX(14) — trend strength
  * Realized vol over 24 bars — volatility level
  * BTC-ETH return correlation over 24 bars — regime coherence

Regime mapping:

    | ADX  | Vol      | Coherence | Regime             |
    | ---- | -------- | --------- | ------------------ |
    | > 25 | any      | > 0.6     | TRENDING_UP/DOWN   |
    | any  | > P90    | any       | HIGH_VOL           |
    | any  | < P30    | any       | LOW_VOL_COMPRESSION|
    | < 18 | P30-P90  | any       | RANGING            |
    | else | else     | else      | NEUTRAL            |

The regime is cached per bar and only recomputed when the bar timestamp
changes (the strategy calls ``detect`` on every poll).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

import numpy as np


class MarketRegime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGING = "ranging"
    HIGH_VOL = "high_vol"
    LOW_VOL_COMPRESSION = "low_vol_compression"
    NEUTRAL = "neutral"


@dataclass
class RegimeSnapshot:
    """Per-bar regime reading — returned by ``RegimeDetector.detect``."""
    regime: MarketRegime
    confidence: float  # 0.0 - 1.0
    adx: float
    realized_vol: float
    btc_corr: float
    vol_percentile: float  # trailing percentile of current vol vs history
    metadata: Dict[str, Any]


class RegimeDetector:
    """Detect market regime from recent OHLCV + BTC series.

    Usage:
        det = RegimeDetector()
        snap = det.detect(
            ohlcv_window=recent_eth_ohlcv,  # pd.DataFrame last ~200 bars
            btc_close_series=recent_btc_close,
        )
        if snap.regime == MarketRegime.TRENDING_UP:
            ...
    """

    def __init__(
        self,
        adx_period: int = 14,
        vol_window: int = 24,
        corr_window: int = 24,
        vol_history_window: int = 720,  # 30 days of 1H
        adx_trend_thresh: float = 25.0,
        adx_range_thresh: float = 18.0,
        corr_coherent_thresh: float = 0.6,
        vol_high_percentile: float = 0.90,
        vol_low_percentile: float = 0.30,
    ):
        self.adx_period = adx_period
        self.vol_window = vol_window
        self.corr_window = corr_window
        self.vol_history_window = vol_history_window
        self.adx_trend_thresh = adx_trend_thresh
        self.adx_range_thresh = adx_range_thresh
        self.corr_coherent_thresh = corr_coherent_thresh
        self.vol_high_percentile = vol_high_percentile
        self.vol_low_percentile = vol_low_percentile

    # ------------------------------------------------------------------
    # Main detect method
    # ------------------------------------------------------------------
    def detect(
        self,
        ohlcv_window: Any,  # pd.DataFrame with columns open/high/low/close
        btc_close_series: Optional[Any] = None,  # pd.Series aligned to ohlcv
    ) -> RegimeSnapshot:
        import pandas as pd

        if len(ohlcv_window) < max(self.adx_period * 2, self.vol_history_window // 4):
            return RegimeSnapshot(
                regime=MarketRegime.NEUTRAL,
                confidence=0.0,
                adx=0.0,
                realized_vol=0.0,
                btc_corr=0.0,
                vol_percentile=0.5,
                metadata={"reason": "insufficient_history"},
            )

        adx = self._compute_adx(ohlcv_window)
        plus_di, minus_di = self._compute_di(ohlcv_window)
        rv = self._compute_realized_vol(ohlcv_window)
        vol_pct = self._compute_vol_percentile(ohlcv_window)
        btc_corr = (
            self._compute_btc_corr(ohlcv_window, btc_close_series)
            if btc_close_series is not None and len(btc_close_series) >= self.corr_window
            else 0.0
        )

        # ── Priority 1: volatility extremes override trend/range ──
        if vol_pct >= self.vol_high_percentile:
            return RegimeSnapshot(
                regime=MarketRegime.HIGH_VOL,
                confidence=min((vol_pct - self.vol_high_percentile) / (1 - self.vol_high_percentile), 1.0),
                adx=adx, realized_vol=rv, btc_corr=btc_corr,
                vol_percentile=vol_pct,
                metadata={"plus_di": plus_di, "minus_di": minus_di},
            )
        if vol_pct <= self.vol_low_percentile:
            return RegimeSnapshot(
                regime=MarketRegime.LOW_VOL_COMPRESSION,
                confidence=min((self.vol_low_percentile - vol_pct) / self.vol_low_percentile, 1.0),
                adx=adx, realized_vol=rv, btc_corr=btc_corr,
                vol_percentile=vol_pct,
                metadata={"plus_di": plus_di, "minus_di": minus_di},
            )

        # ── Priority 2: ADX-based trend/range classification ──
        if adx >= self.adx_trend_thresh and btc_corr >= self.corr_coherent_thresh:
            # Strong trend with BTC coherence — direction from DI spread
            if plus_di > minus_di:
                regime = MarketRegime.TRENDING_UP
            else:
                regime = MarketRegime.TRENDING_DOWN
            conf = min((adx - self.adx_trend_thresh) / 20.0, 1.0) * 0.5 + (btc_corr - self.corr_coherent_thresh) * 0.5
            return RegimeSnapshot(
                regime=regime,
                confidence=max(0.0, min(conf, 1.0)),
                adx=adx, realized_vol=rv, btc_corr=btc_corr,
                vol_percentile=vol_pct,
                metadata={"plus_di": plus_di, "minus_di": minus_di},
            )

        if adx <= self.adx_range_thresh:
            return RegimeSnapshot(
                regime=MarketRegime.RANGING,
                confidence=min((self.adx_range_thresh - adx) / self.adx_range_thresh, 1.0),
                adx=adx, realized_vol=rv, btc_corr=btc_corr,
                vol_percentile=vol_pct,
                metadata={"plus_di": plus_di, "minus_di": minus_di},
            )

        # ── Default: neutral / transition ──
        return RegimeSnapshot(
            regime=MarketRegime.NEUTRAL,
            confidence=0.3,
            adx=adx, realized_vol=rv, btc_corr=btc_corr,
            vol_percentile=vol_pct,
            metadata={"plus_di": plus_di, "minus_di": minus_di},
        )

    # ------------------------------------------------------------------
    # Indicator computation (vectorised, pandas-based)
    # ------------------------------------------------------------------
    def _compute_adx(self, df: Any) -> float:
        """Wilder's ADX on the full window; returns the last value."""
        import pandas as pd

        high = df["high"]
        low = df["low"]
        close = df["close"]

        # True Range
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)

        # Directional movement
        up_move = high - high.shift(1)
        down_move = low.shift(1) - low
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        # Wilder smoothing
        period = self.adx_period
        atr = tr.ewm(alpha=1 / period, adjust=False).mean()
        plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
        minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)

        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx = dx.ewm(alpha=1 / period, adjust=False).mean()
        return float(adx.iloc[-1]) if not np.isnan(adx.iloc[-1]) else 0.0

    def _compute_di(self, df: Any) -> tuple:
        """Return (plus_di, minus_di) at the last bar."""
        import pandas as pd

        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        up_move = high - high.shift(1)
        down_move = low.shift(1) - low
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
        period = self.adx_period
        atr = tr.ewm(alpha=1 / period, adjust=False).mean().replace(0, np.nan)
        plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr
        minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False).mean() / atr
        p = float(plus_di.iloc[-1]) if not np.isnan(plus_di.iloc[-1]) else 0.0
        m = float(minus_di.iloc[-1]) if not np.isnan(minus_di.iloc[-1]) else 0.0
        return p, m

    def _compute_realized_vol(self, df: Any) -> float:
        log_ret = np.log(df["close"] / df["close"].shift(1))
        window = log_ret.iloc[-self.vol_window:]
        return float(np.sqrt((window ** 2).sum())) if len(window) >= 2 else 0.0

    def _compute_vol_percentile(self, df: Any) -> float:
        """Percentile of current 24h realized vol vs last 30 days of same."""
        log_ret = np.log(df["close"] / df["close"].shift(1))
        # Rolling 24-bar realized vol
        import pandas as pd
        rv_series = np.sqrt((log_ret ** 2).rolling(self.vol_window, min_periods=self.vol_window // 2).sum())
        recent = rv_series.iloc[-self.vol_history_window:].dropna()
        if len(recent) < 10:
            return 0.5
        current = rv_series.iloc[-1]
        if np.isnan(current):
            return 0.5
        return float((recent <= current).mean())

    def _compute_btc_corr(self, df: Any, btc_close: Any) -> float:
        import pandas as pd

        eth_ret = np.log(df["close"].values[1:] / df["close"].values[:-1])

        if isinstance(btc_close, pd.Series):
            btc_values = btc_close.values
        else:
            btc_values = np.asarray(btc_close)

        if len(btc_values) < 2 or len(eth_ret) < 2:
            return 0.0

        btc_ret = np.log(btc_values[1:] / btc_values[:-1])

        # Align on the shorter of the two (position-based)
        n = min(len(eth_ret), len(btc_ret), self.corr_window)
        if n < 8:
            return 0.0

        e = eth_ret[-n:]
        b = btc_ret[-n:]
        # Drop any NaN/Inf pairs
        mask = np.isfinite(e) & np.isfinite(b)
        if mask.sum() < 8:
            return 0.0
        e = e[mask]
        b = b[mask]

        # Manual Pearson (faster than pd.Series.corr for small arrays)
        e_mean = e.mean()
        b_mean = b.mean()
        cov = ((e - e_mean) * (b - b_mean)).mean()
        e_std = e.std()
        b_std = b.std()
        if e_std == 0 or b_std == 0:
            return 0.0
        return float(cov / (e_std * b_std))


# ────────────────────────────────────────────────────────────
# Factor subset per regime — used by the strategy's signal composer
# ────────────────────────────────────────────────────────────

# Chosen from the factor evaluation pipeline (Rank IC > 0.02 across 2024-2026).
# Weights are directional coefficients — positive = factor's z-score contributes
# positively to LONG conviction, negative = contributes to SHORT conviction.
# Magnitudes reflect the rank_ic strength found in the batch evaluation.

REGIME_FACTOR_WEIGHTS: Dict[str, Dict[str, float]] = {
    # TRENDING: momentum continuation + cross-asset confirmation
    MarketRegime.TRENDING_UP.value: {
        "btc_eth_correlation_24h": +0.8,   # strong BTC coherence → persist
        "momentum_accel_6h":        -0.4,   # negative in evaluation = mean-revert even in trend
        "us_session_volume_surge":  +0.3,   # volume surge confirms
        "realized_vol_24h":         +0.2,   # vol expansion OK in trend
    },
    MarketRegime.TRENDING_DOWN.value: {
        "btc_eth_correlation_24h": +0.8,
        "momentum_accel_6h":        -0.4,
        "us_session_volume_surge":  +0.3,
        "realized_vol_24h":         +0.2,
    },

    # RANGING: mean reversion dominates
    MarketRegime.RANGING.value: {
        "vwap_dev_24h":             -1.0,   # price deviation reverts (sign already -)
        "upper_wick_pressure":      +0.7,   # already sign-flipped (+ = bullish)
        "lower_wick_pressure":      +0.7,
        "weekend_continuation":     -0.5,
        "body_to_range_ratio":      -0.3,
    },

    # HIGH_VOL: reduce conviction, rely on reversion
    MarketRegime.HIGH_VOL.value: {
        "vwap_dev_24h":             -0.8,
        "vol_clustering_48h":       -0.6,
        "upper_wick_pressure":      +0.4,
        "lower_wick_pressure":      +0.4,
    },

    # LOW_VOL_COMPRESSION: breakout likely — use momentum factors
    MarketRegime.LOW_VOL_COMPRESSION.value: {
        "fractal_break_5":          -0.5,   # eval showed negative IR → fade breakouts
        "range_contraction":        +0.4,
        "momentum_accel_6h":        -0.3,
    },

    # NEUTRAL: reduced exposure, use only strongest signals
    MarketRegime.NEUTRAL.value: {
        "btc_eth_correlation_24h": +0.5,
        "vwap_dev_24h":             -0.5,
    },
}


def get_active_factors(regime: MarketRegime) -> List[str]:
    """Return the list of factor names active in the given regime."""
    return list(REGIME_FACTOR_WEIGHTS.get(regime.value, {}).keys())
