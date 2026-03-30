"""Macro cycle signal computation from OHLCV price data.

Computes proxy signals for the multi-factor BTC/ETH macro cycle dashboard:

1. MA200W Score   — Distance from 200-week MA (accumulation zone detection)
2. Pi Cycle Score — 111 DMA vs 350 DMA × 2 (cycle top/bottom indicator)
3. Trend Score    — Triple EMA alignment + ADX strength
4. Momentum Score — ROC + RSI regime detection
5. Volume Score   — OBV slope + volume trend (accumulation vs distribution)
6. Drawdown Score — Distance from ATH (contrarian mean-reversion signal)
7. Volatility Regime — Bollinger Band width (coiling = imminent breakout)

Each signal outputs a value in [-1, +1]:
    +1 = strongly bullish
     0 = neutral
    -1 = strongly bearish

Composite score = weighted sum of all signals, clipped to [-1, +1].
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np


@dataclass
class MacroSignalState:
    """Mutable state for incremental signal computation."""

    # Price history (need ~1500 bars of 4H data for 200W MA proxy)
    closes: Deque[float] = field(default_factory=lambda: deque(maxlen=2000))
    highs: Deque[float] = field(default_factory=lambda: deque(maxlen=2000))
    lows: Deque[float] = field(default_factory=lambda: deque(maxlen=2000))
    volumes: Deque[float] = field(default_factory=lambda: deque(maxlen=2000))

    # EMAs (updated incrementally)
    ema_21: float = 0.0
    ema_55: float = 0.0
    ema_200: float = 0.0

    # Moving averages for Pi Cycle
    dma_111_sum: float = 0.0
    dma_350_sum: float = 0.0

    # OBV
    obv: float = 0.0
    obv_history: Deque[float] = field(default_factory=lambda: deque(maxlen=200))

    # ATH tracking
    all_time_high: float = 0.0

    # Bar count
    n_bars: int = 0
    initialized: bool = False


def _ema_update(prev: float, price: float, period: int) -> float:
    alpha = 2.0 / (period + 1)
    return alpha * price + (1 - alpha) * prev


def _sma(values: list, period: int) -> float:
    if len(values) < period:
        return np.mean(values) if values else 0.0
    return np.mean(values[-period:])


def update_state(state: MacroSignalState, close: float, high: float,
                 low: float, volume: float) -> None:
    """Update state with a new bar. Call once per bar."""
    prev_close = state.closes[-1] if state.closes else close

    state.closes.append(close)
    state.highs.append(high)
    state.lows.append(low)
    state.volumes.append(volume)
    state.n_bars += 1

    # Update EMAs
    if state.n_bars == 1:
        state.ema_21 = close
        state.ema_55 = close
        state.ema_200 = close
    else:
        state.ema_21 = _ema_update(state.ema_21, close, 21)
        state.ema_55 = _ema_update(state.ema_55, close, 55)
        state.ema_200 = _ema_update(state.ema_200, close, 200)

    # Update OBV
    if close > prev_close:
        state.obv += volume
    elif close < prev_close:
        state.obv -= volume
    state.obv_history.append(state.obv)

    # Update ATH
    if high > state.all_time_high:
        state.all_time_high = high

    # Mark initialized after enough bars (200W ≈ 1400 bars on 4H, but we relax to 250)
    if state.n_bars >= 250 and not state.initialized:
        state.initialized = True


# ============================================================
# Individual Signal Computations
# ============================================================

def compute_ma200w_score(state: MacroSignalState) -> float:
    """200-Week MA proximity score.

    On 4H bars, 200 weeks ≈ 200 * 7 * 6 = 8400 bars (too many).
    We use a practical proxy: 200-bar SMA on daily aggregation ≈ 200 EMA on 4H.
    For 4H data: 200-day MA ≈ 1200 bars, we use EMA-200 as proxy.

    Score: distance from EMA-200 normalized by ATR.
        Near/below EMA-200 → +1 (accumulation zone, bullish)
        Far above EMA-200 → -0.5 to -1 (overextended, caution)
        Moderately above → +0.3 (healthy trend)
    """
    if state.n_bars < 200:
        return 0.0

    price = state.closes[-1]
    ma = state.ema_200

    if ma == 0:
        return 0.0

    # Distance as percentage
    dist_pct = (price - ma) / ma

    if dist_pct < -0.10:
        # Deep below MA → strong accumulation signal
        return 1.0
    elif dist_pct < -0.02:
        # Slightly below MA → accumulation zone
        return 0.7
    elif dist_pct < 0.05:
        # Near MA → neutral to slightly bullish
        return 0.3
    elif dist_pct < 0.20:
        # Moderately above → healthy trend
        return 0.1
    elif dist_pct < 0.50:
        # Extended above → caution
        return -0.3
    else:
        # Very extended → overheated
        return -0.7


def compute_pi_cycle_score(state: MacroSignalState) -> float:
    """Pi Cycle Top Indicator: 111 DMA vs 350 DMA × 2.

    On 4H bars: 111 days ≈ 666 bars, 350 days ≈ 2100 bars.
    We adapt: use 111-bar SMA vs 350-bar SMA × 2 (scaled for 4H timeframe).
    Actually for 4H: 111-day = 111*6 = 666 bars, 350-day = 350*6 = 2100.
    Too many bars. Let's use a scaled version: 111-bar and 350-bar SMAs directly.

    When 111 SMA crosses above 350 SMA × 2 → cycle top (bearish)
    When far apart (111 << 350*2) → room to run (bullish)
    """
    closes = list(state.closes)

    if len(closes) < 350:
        return 0.0

    sma_111 = np.mean(closes[-111:])
    sma_350 = np.mean(closes[-350:])
    sma_350x2 = sma_350 * 2

    if sma_350x2 == 0:
        return 0.0

    # Ratio: how close are the two lines?
    ratio = sma_111 / sma_350x2

    if ratio > 1.0:
        # Crossed above → cycle top signal
        return -1.0
    elif ratio > 0.95:
        # Very close to crossing → strong bearish warning
        return -0.7
    elif ratio > 0.85:
        # Getting closer → moderate caution
        return -0.3
    elif ratio > 0.70:
        # Moderate gap → healthy cycle progression
        return 0.3
    elif ratio > 0.50:
        # Large gap → early/mid cycle → strong bullish
        return 0.7
    else:
        # Very large gap → deep accumulation
        return 1.0


def compute_trend_score(state: MacroSignalState) -> float:
    """Triple EMA alignment + ADX-like directional strength.

    EMA 21 > EMA 55 > EMA 200 → strong uptrend → +1
    EMA 21 < EMA 55 < EMA 200 → strong downtrend → -1
    Mixed → neutral (0)
    """
    if state.n_bars < 200:
        return 0.0

    price = state.closes[-1]
    e21 = state.ema_21
    e55 = state.ema_55
    e200 = state.ema_200

    # Perfect bullish alignment
    if price > e21 > e55 > e200:
        # Strength based on separation
        spread = (e21 - e200) / e200 if e200 > 0 else 0
        return min(0.5 + spread * 5, 1.0)

    # Perfect bearish alignment
    if price < e21 < e55 < e200:
        spread = (e200 - e21) / e200 if e200 > 0 else 0
        return max(-0.5 - spread * 5, -1.0)

    # Partial bullish
    if e21 > e55 and price > e200:
        return 0.3

    # Partial bearish
    if e21 < e55 and price < e200:
        return -0.3

    # Mixed/transitional
    return 0.0


def compute_momentum_score(state: MacroSignalState) -> float:
    """ROC (Rate of Change) + RSI regime detection.

    Combines 20-bar and 60-bar momentum with RSI overbought/oversold zones.
    """
    closes = list(state.closes)

    if len(closes) < 61:
        return 0.0

    price = closes[-1]

    # ROC 20 and ROC 60
    roc_20 = (price - closes[-21]) / closes[-21] if closes[-21] != 0 else 0
    roc_60 = (price - closes[-61]) / closes[-61] if closes[-61] != 0 else 0

    # RSI 14
    rsi = _compute_rsi(closes, 14)

    # ROC score: normalize
    roc_score = np.clip(roc_20 * 10, -1, 1) * 0.4 + np.clip(roc_60 * 5, -1, 1) * 0.3

    # RSI score
    if rsi > 80:
        rsi_score = -0.8  # Overbought → bearish
    elif rsi > 70:
        rsi_score = -0.3
    elif rsi < 20:
        rsi_score = 0.8   # Oversold → bullish (contrarian)
    elif rsi < 30:
        rsi_score = 0.5
    elif 45 < rsi < 55:
        rsi_score = 0.0   # Neutral
    elif rsi >= 55:
        rsi_score = 0.1   # Slightly bullish momentum
    else:
        rsi_score = -0.1

    return np.clip(roc_score + rsi_score * 0.3, -1, 1)


def compute_volume_score(state: MacroSignalState) -> float:
    """OBV slope + volume trend analysis.

    Rising OBV = accumulation → bullish
    Falling OBV = distribution → bearish
    Volume surge (>2x average) on up-move → very bullish
    """
    if len(state.obv_history) < 30 or len(state.volumes) < 30:
        return 0.0

    obv_list = list(state.obv_history)
    volumes = list(state.volumes)

    # OBV slope (20-bar linear regression slope direction)
    obv_recent = obv_list[-20:]
    x = np.arange(len(obv_recent))
    if np.std(obv_recent) > 0:
        slope = np.polyfit(x, obv_recent, 1)[0]
        obv_range = max(obv_list) - min(obv_list)
        if obv_range > 0:
            obv_score = np.clip(slope / (obv_range / 100), -1, 1)
        else:
            obv_score = 0.0
    else:
        obv_score = 0.0

    # Volume surge detection
    avg_vol = np.mean(volumes[-30:])
    recent_vol = np.mean(volumes[-5:])
    vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0

    # Price direction during volume surge
    price_chg = (state.closes[-1] - state.closes[-6]) / state.closes[-6] if state.closes[-6] != 0 else 0

    vol_score = 0.0
    if vol_ratio > 2.0 and price_chg > 0.01:
        vol_score = 0.5   # Volume surge on up-move → bullish
    elif vol_ratio > 2.0 and price_chg < -0.01:
        vol_score = -0.5  # Volume surge on down-move → bearish
    elif vol_ratio > 1.5 and price_chg > 0:
        vol_score = 0.2
    elif vol_ratio > 1.5 and price_chg < 0:
        vol_score = -0.2

    return np.clip(obv_score * 0.6 + vol_score * 0.4, -1, 1)


def compute_drawdown_score(state: MacroSignalState) -> float:
    """Distance from ATH as contrarian signal.

    Deep drawdown from ATH → accumulation zone → bullish
    Near ATH → either strong trend or potential top → neutral to slight caution
    At new ATH → momentum continuation (but watch other signals)
    """
    if state.all_time_high == 0 or state.n_bars < 50:
        return 0.0

    price = state.closes[-1]
    dd_pct = (state.all_time_high - price) / state.all_time_high

    if dd_pct > 0.70:
        return 1.0     # >70% from ATH → extreme accumulation zone
    elif dd_pct > 0.50:
        return 0.8     # 50-70% → strong accumulation
    elif dd_pct > 0.30:
        return 0.5     # 30-50% → moderate accumulation
    elif dd_pct > 0.15:
        return 0.2     # 15-30% → healthy correction
    elif dd_pct > 0.05:
        return 0.0     # 5-15% → normal pullback
    elif dd_pct > 0.01:
        return -0.1    # Near ATH → slight caution
    else:
        return 0.1     # At or above ATH → momentum continuation


def compute_volatility_regime_score(state: MacroSignalState) -> float:
    """Bollinger Band width as volatility regime proxy.

    Low vol (tight bands) after extended period → coiling → imminent breakout → bullish
    High vol spike → capitulation or blow-off top (check direction)
    """
    closes = list(state.closes)

    if len(closes) < 50:
        return 0.0

    # Bollinger Band width (20-period)
    sma_20 = np.mean(closes[-20:])
    std_20 = np.std(closes[-20:])
    bb_width = (std_20 / sma_20) if sma_20 > 0 else 0

    # Historical BB width for percentile ranking
    bb_widths = []
    for i in range(50, len(closes)):
        s = np.mean(closes[i-20:i])
        d = np.std(closes[i-20:i])
        if s > 0:
            bb_widths.append(d / s)

    if not bb_widths:
        return 0.0

    # Percentile of current BB width
    percentile = np.mean([1 if w <= bb_width else 0 for w in bb_widths])

    # Price trend during low vol
    price_trend = (closes[-1] - closes[-20]) / closes[-20] if closes[-20] != 0 else 0

    if percentile < 0.15:
        # Very low vol → coiling → bullish (breakout imminent)
        return 0.6 if price_trend >= 0 else 0.3
    elif percentile < 0.30:
        # Low vol → accumulation
        return 0.3
    elif percentile > 0.90:
        # Extreme vol → capitulation or blow-off
        if price_trend < -0.05:
            return 0.5   # Capitulation → contrarian bullish
        elif price_trend > 0.05:
            return -0.5  # Blow-off top → bearish
        return 0.0
    elif percentile > 0.75:
        # High vol
        return -0.2
    else:
        return 0.0


def _compute_rsi(closes: list, period: int = 14) -> float:
    """Compute RSI from close prices."""
    if len(closes) < period + 1:
        return 50.0
    recent = closes[-(period + 1):]
    deltas = [recent[i] - recent[i-1] for i in range(1, len(recent))]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_gain = np.mean(gains) if gains else 0
    avg_loss = np.mean(losses) if losses else 0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


# ============================================================
# Composite Score
# ============================================================

# Default weights (can be overridden via config)
DEFAULT_WEIGHTS = {
    "ma200w":     0.15,
    "pi_cycle":   0.10,
    "trend":      0.25,
    "momentum":   0.20,
    "volume":     0.10,
    "drawdown":   0.10,
    "volatility": 0.10,
}


def compute_composite_score(
    state: MacroSignalState,
    weights: Optional[Dict[str, float]] = None,
) -> Tuple[float, Dict[str, float]]:
    """Compute weighted composite score from all macro signals.

    Returns:
        (composite_score, individual_scores_dict)
        composite_score in [-1, +1]
    """
    w = weights or DEFAULT_WEIGHTS

    scores = {
        "ma200w":     compute_ma200w_score(state),
        "pi_cycle":   compute_pi_cycle_score(state),
        "trend":      compute_trend_score(state),
        "momentum":   compute_momentum_score(state),
        "volume":     compute_volume_score(state),
        "drawdown":   compute_drawdown_score(state),
        "volatility": compute_volatility_regime_score(state),
    }

    composite = sum(scores[k] * w.get(k, 0) for k in scores)
    composite = float(np.clip(composite, -1, 1))

    return composite, scores


def compute_atr(state: MacroSignalState, period: int = 14) -> float:
    """Compute ATR from state."""
    if state.n_bars < period + 1:
        return 0.0

    highs = list(state.highs)
    lows = list(state.lows)
    closes = list(state.closes)

    trs = []
    for i in range(-period, 0):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)

    return float(np.mean(trs))
