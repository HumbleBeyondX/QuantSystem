"""OHLCV-based proxies for microstructure factors.

When historical orderbook data is unavailable (most common for retail),
these functions approximate microstructure factors from standard OHLCV bars.
This allows backtesting factor alpha before committing to expensive
orderbook data collection or paper trading validation.

Proxy quality ranking (best → worst):
    VPIN     — Native bar-based, no proxy needed (exact)
    TFI      — BVC classification is well-studied (good proxy)
    OBI      — Price position + volume signal (moderate proxy)
    Whale    — Volume spikes relative to median (moderate proxy)
    Spread   — ATR-based estimate (rough proxy)
    Microprice — Intrabar price asymmetry (rough proxy)

Reference:
    Easley, López de Prado, O'Hara (2012) "Flow Toxicity and Liquidity
    in a High-frequency World" — BVC for VPIN estimation from bars.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, List, Optional

import numpy as np
import pandas as pd


def compute_obi_proxy(
    bars: List[Dict[str, float]],
    lookback: int = 12,
) -> float:
    """Approximate OBI from OHLCV bar data.

    Proxy logic: The position of close within [low, high] indicates
    buying vs selling pressure. Weighted by volume.

    close_position = (close - low) / (high - low)  → 0=sell pressure, 1=buy pressure
    Volume-weighted average over recent bars.

    Args:
        bars: List of bar dicts with OHLCV fields.
        lookback: Number of recent bars to consider.

    Returns:
        Proxy OBI in [-1, 1].
    """
    if not bars:
        return 0.0

    recent = bars[-lookback:]
    weighted_sum = 0.0
    total_vol = 0.0

    for bar in recent:
        h, l, c = bar["high"], bar["low"], bar["close"]
        v = bar.get("volume", 1.0)

        if h <= l or v <= 0:
            continue

        # Close position: 0 → sold off (close=low), 1 → bought up (close=high)
        close_pos = (c - l) / (h - l)
        # Map to [-1, 1]: 0.5 → 0 (neutral), 1 → +1 (buy), 0 → -1 (sell)
        imbalance = 2.0 * close_pos - 1.0

        weighted_sum += imbalance * v
        total_vol += v

    if total_vol <= 0:
        return 0.0

    return float(np.clip(weighted_sum / total_vol, -1.0, 1.0))


def compute_tfi_proxy(
    bars: List[Dict[str, float]],
    lookback: int = 6,
) -> float:
    """Approximate TFI using Bulk Volume Classification (BVC).

    BVC formula: buy_volume = volume × Φ(Z), where Z = (close - open) / (high - low)
    This is the standard academic approach for classifying volume direction
    from bar data (Easley et al., 2012).

    Args:
        bars: List of bar dicts with OHLCV fields.
        lookback: Number of recent bars.

    Returns:
        Proxy TFI in [-1, 1].
    """
    if not bars:
        return 0.0

    recent = bars[-lookback:]
    total_buy = 0.0
    total_sell = 0.0

    for bar in recent:
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        v = bar.get("volume", 0)

        if v <= 0:
            continue

        price_range = h - l
        if price_range > 0:
            z = (c - o) / price_range
            buy_pct = 0.5 * (1.0 + np.clip(z, -1.0, 1.0))
        else:
            buy_pct = 0.5

        total_buy += v * buy_pct
        total_sell += v * (1.0 - buy_pct)

    total = total_buy + total_sell
    if total <= 0:
        return 0.0

    return float(np.clip((total_buy - total_sell) / total, -1.0, 1.0))


def compute_spread_proxy(
    bars: List[Dict[str, float]],
    lookback: int = 50,
) -> float:
    """Approximate spread z-score from ATR-based spread estimate.

    Proxy: effective_spread ≈ 2 × (high - low) / (high + low) × 10000 (bps)
    Then compute z-score vs rolling mean.

    This is a rough proxy — actual spread is much tighter than high-low range,
    but the relative dynamics (tight vs wide periods) are captured.

    Args:
        bars: List of bar dicts.
        lookback: Rolling window for z-score.

    Returns:
        Z-score of current spread estimate. Negative = tighter than normal.
    """
    if len(bars) < lookback:
        return 0.0

    spreads = []
    for bar in bars[-lookback:]:
        h, l = bar["high"], bar["low"]
        mid = (h + l) / 2.0
        if mid > 0:
            spread_bps = (h - l) / mid * 10_000
            spreads.append(spread_bps)

    if len(spreads) < 10:
        return 0.0

    arr = np.array(spreads)
    mean_s = arr.mean()
    std_s = arr.std()

    if std_s < 1e-8:
        return 0.0

    current = spreads[-1]
    z = (current - mean_s) / std_s

    return float(np.clip(z, -3.0, 3.0))


def compute_microprice_proxy(
    bars: List[Dict[str, float]],
    lookback: int = 6,
) -> float:
    """Approximate microprice deviation from intrabar price asymmetry.

    Proxy: If close > (open + close) / 2, buy-side pressure dominates.
    We measure the weighted average deviation of close from the bar midpoint.

    Returns value in approximate basis points (scaled for composite compatibility).
    """
    if not bars:
        return 0.0

    recent = bars[-lookback:]
    deviations = []

    for bar in recent:
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        v = bar.get("volume", 1.0)

        bar_mid = (o + c) / 2.0
        range_mid = (h + l) / 2.0

        if range_mid > 0:
            # How far close deviates from range midpoint, in bps
            dev_bps = (c - range_mid) / range_mid * 10_000
            deviations.append(dev_bps)

    if not deviations:
        return 0.0

    return float(np.mean(deviations))


def compute_whale_proxy(
    bars: List[Dict[str, float]],
    lookback_median: int = 100,
    multiplier: float = 3.0,
    recent_bars: int = 3,
) -> float:
    """Approximate whale activity from volume spikes.

    Proxy: A bar with volume > multiplier × rolling_median_volume is a
    "whale bar". The direction is inferred from the bar's close vs open.

    Note: multiplier is lower (3x vs 10x for real trades) because bar volume
    aggregates many trades, so the threshold must be lower.

    Returns:
        Whale imbalance proxy in [-1, 1].
    """
    if len(bars) < lookback_median:
        return 0.0

    volumes = [bar.get("volume", 0) for bar in bars[-lookback_median:]]
    median_vol = np.median(volumes)

    if median_vol <= 0:
        return 0.0

    threshold = median_vol * multiplier
    whale_buy = 0.0
    whale_sell = 0.0

    for bar in bars[-recent_bars:]:
        v = bar.get("volume", 0)
        if v < threshold:
            continue

        # Infer direction from bar direction
        if bar["close"] > bar["open"]:
            whale_buy += v
        elif bar["close"] < bar["open"]:
            whale_sell += v

    total = whale_buy + whale_sell
    if total <= 0:
        return 0.0

    return float(np.clip((whale_buy - whale_sell) / total, -1.0, 1.0))


def compute_all_proxies(
    bars: List[Dict[str, float]],
    vpin_n_buckets: int = 50,
) -> Dict[str, float]:
    """Compute all 6 factor proxies from OHLCV bars.

    Convenience function that runs all proxy computations and returns
    a dict compatible with compute_composite_score().

    Args:
        bars: List of bar dicts with OHLCV fields. Need >= 100 bars for full warmup.
        vpin_n_buckets: Number of buckets for VPIN computation.

    Returns:
        Dict with keys: obi, tfi, spread_zscore, microprice_dev, whale_imbalance, vpin.
    """
    from data.features.microstructure_midfreq import compute_vpin_bars

    return {
        "obi": compute_obi_proxy(bars, lookback=12),
        "tfi": compute_tfi_proxy(bars, lookback=6),
        "spread_zscore": compute_spread_proxy(bars, lookback=50),
        "microprice_dev": compute_microprice_proxy(bars, lookback=6),
        "whale_imbalance": compute_whale_proxy(bars, lookback_median=100, multiplier=3.0),
        "vpin": compute_vpin_bars(bars, n_buckets=vpin_n_buckets),
    }
