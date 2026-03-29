"""Mid-frequency microstructure factors for crypto trading.

Unlike the tick-level microstructure.py, these factors aggregate orderbook
snapshots and trade data over bar-length windows (5min-15min). Designed for
retail traders who cannot compete on latency but can exploit information
aggregation over longer horizons.

Factors:
    1. OBI  - Order Book Imbalance (multi-level, time-averaged)
    2. TFI  - Trade Flow Imbalance (buy vs sell aggressor volume)
    3. Spread Z-Score - relative spread dynamics
    4. Microprice Deviation - microprice vs mid divergence
    5. Whale Signal - large trade detection
    6. VPIN (reused from microstructure.py via bar-based computation)
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np

from core.logging import get_logger

logger = get_logger(__name__)


# ============================================================
# Factor 1: Order Book Imbalance (Multi-Level, Time-Averaged)
# ============================================================


def compute_obi_midfreq(
    snapshots: List[Dict[str, Any]],
    levels: int = 10,
    decay: float = 0.94,
) -> float:
    """Compute EMA-smoothed order book imbalance over accumulated snapshots.

    Each snapshot should have:
        - "bids": List of [price, qty] pairs (descending by price)
        - "asks": List of [price, qty] pairs (ascending by price)

    Unlike a single-snapshot OBI, this aggregates across all snapshots
    collected during one bar interval, using exponential weighting to
    emphasize recent snapshots.

    Args:
        snapshots: List of orderbook snapshot dicts from the bar period.
        levels: Number of depth levels to consider.
        decay: EMA decay factor (0.94 over ~20 snapshots gives half-life ~12).

    Returns:
        Smoothed OBI in [-1, 1]. Positive = buy pressure.
    """
    if not snapshots:
        return 0.0

    imbalances = []
    for snap in snapshots:
        bids = snap.get("bids", [])
        asks = snap.get("asks", [])

        bid_depth = sum(qty for _, qty in bids[:levels])
        ask_depth = sum(qty for _, qty in asks[:levels])
        total = bid_depth + ask_depth

        if total > 0:
            imbalances.append((bid_depth - ask_depth) / total)
        else:
            imbalances.append(0.0)

    if not imbalances:
        return 0.0

    # Exponential weighted mean (recent snapshots weighted more)
    ema = imbalances[0]
    for val in imbalances[1:]:
        ema = decay * ema + (1 - decay) * val

    return float(np.clip(ema, -1.0, 1.0))


# ============================================================
# Factor 2: Trade Flow Imbalance
# ============================================================


def compute_tfi_midfreq(
    trades: List[Dict[str, Any]],
    window_seconds: int = 300,
) -> float:
    """Compute buy vs sell aggressor volume imbalance.

    Each trade should have:
        - "side": "buy" or "sell" (from exchange trade tape)
        - "amount": trade quantity
        - "timestamp": epoch seconds (optional, for windowing)

    Args:
        trades: List of trade dicts from the bar period.
        window_seconds: Only consider trades within this window (if timestamps provided).

    Returns:
        TFI in [-1, 1]. Positive = net buying pressure.
    """
    if not trades:
        return 0.0

    buy_vol = 0.0
    sell_vol = 0.0

    for trade in trades:
        amount = float(trade.get("amount", 0))
        side = trade.get("side", "").lower()

        if side == "buy":
            buy_vol += amount
        elif side == "sell":
            sell_vol += amount

    total = buy_vol + sell_vol
    if total == 0:
        return 0.0

    return float(np.clip((buy_vol - sell_vol) / total, -1.0, 1.0))


# ============================================================
# Factor 3: Spread Z-Score
# ============================================================


def compute_spread_zscore(
    current_spread_bps: float,
    spread_history: Deque[float],
    lookback: int = 50,
) -> float:
    """Compute z-score of current spread vs rolling mean.

    Negative z-score = spread tighter than normal = favorable entry.
    Positive z-score = spread wider than normal = adverse conditions.

    Args:
        current_spread_bps: Current bar's average spread in basis points.
        spread_history: Deque of historical spread values (bps).
        lookback: Number of bars for rolling stats.

    Returns:
        Z-score of spread. Negative = tight (good for trading).
    """
    if len(spread_history) < max(5, lookback // 2):
        return 0.0

    recent = list(spread_history)[-lookback:]
    mean_spread = np.mean(recent)
    std_spread = np.std(recent)

    if std_spread < 1e-8:
        return 0.0

    return float(np.clip((current_spread_bps - mean_spread) / std_spread, -3.0, 3.0))


# ============================================================
# Factor 4: Microprice Deviation
# ============================================================


def compute_microprice_deviation(
    snapshots: List[Dict[str, Any]],
) -> float:
    """Compute average (microprice - mid) / mid in basis points.

    When microprice consistently exceeds mid, buy-side is dominant.

    Each snapshot should have:
        - "best_bid": float
        - "best_ask": float
        - "bid_qty": float (top-of-book bid quantity)
        - "ask_qty": float (top-of-book ask quantity)

    Returns:
        Average deviation in basis points. Positive = buy pressure.
    """
    if not snapshots:
        return 0.0

    deviations = []
    for snap in snapshots:
        best_bid = snap.get("best_bid", 0)
        best_ask = snap.get("best_ask", 0)
        bid_qty = snap.get("bid_qty", 0)
        ask_qty = snap.get("ask_qty", 0)

        if best_bid <= 0 or best_ask <= 0:
            continue

        mid = (best_bid + best_ask) / 2.0
        total_qty = bid_qty + ask_qty

        if total_qty > 0 and mid > 0:
            microprice = (best_bid * ask_qty + best_ask * bid_qty) / total_qty
            deviation_bps = (microprice - mid) / mid * 10_000
            deviations.append(deviation_bps)

    if not deviations:
        return 0.0

    return float(np.mean(deviations))


# ============================================================
# Factor 5: Large Trade Detection (Whale Signal)
# ============================================================


def compute_whale_signal(
    trades: List[Dict[str, Any]],
    median_amount: float,
    multiplier: float = 10.0,
) -> Dict[str, float]:
    """Detect large (whale) trades and compute directional imbalance.

    Args:
        trades: List of trade dicts with "side" and "amount".
        median_amount: Rolling median trade size for this symbol.
        multiplier: Threshold multiplier (trade > multiplier * median = whale).

    Returns:
        Dict with "whale_buy_vol", "whale_sell_vol", "whale_imbalance" in [-1, 1].
    """
    result = {"whale_buy_vol": 0.0, "whale_sell_vol": 0.0, "whale_imbalance": 0.0}

    if not trades or median_amount <= 0:
        return result

    threshold = median_amount * multiplier
    whale_buy = 0.0
    whale_sell = 0.0

    for trade in trades:
        amount = float(trade.get("amount", 0))
        if amount < threshold:
            continue

        side = trade.get("side", "").lower()
        if side == "buy":
            whale_buy += amount
        elif side == "sell":
            whale_sell += amount

    result["whale_buy_vol"] = whale_buy
    result["whale_sell_vol"] = whale_sell

    total_whale = whale_buy + whale_sell
    if total_whale > 0:
        result["whale_imbalance"] = float(
            np.clip((whale_buy - whale_sell) / total_whale, -1.0, 1.0)
        )

    return result


# ============================================================
# Factor 6: VPIN (simplified, bar-based)
# ============================================================


def compute_vpin_bars(
    bars: List[Dict[str, Any]],
    n_buckets: int = 50,
) -> float:
    """Simplified VPIN computed from OHLCV bars using BVC classification.

    Uses Bulk Volume Classification (BVC) to estimate buy/sell volume
    from bar data without needing tick-level trade classification.

    BVC formula: V_buy = V * CDF(Z), where Z = (close - open) / (high - low)

    Args:
        bars: List of bar dicts with "open", "high", "low", "close", "volume".
        n_buckets: Number of volume buckets for VPIN computation.

    Returns:
        VPIN in [0, 1]. Higher = more informed trading activity.
    """
    if len(bars) < n_buckets:
        return 0.0

    # Use most recent n_buckets bars
    recent = bars[-n_buckets:]

    buy_volumes = []
    sell_volumes = []

    for bar in recent:
        o, h, l, c, v = (
            bar.get("open", 0),
            bar.get("high", 0),
            bar.get("low", 0),
            bar.get("close", 0),
            bar.get("volume", 0),
        )

        if v <= 0:
            continue

        price_range = h - l
        if price_range > 0:
            z = (c - o) / price_range
            # Approximate CDF with sigmoid-like function
            buy_pct = 0.5 * (1 + z)
        else:
            buy_pct = 0.5

        buy_pct = np.clip(buy_pct, 0.0, 1.0)
        buy_volumes.append(v * buy_pct)
        sell_volumes.append(v * (1 - buy_pct))

    if not buy_volumes:
        return 0.0

    # VPIN = mean(|V_buy - V_sell|) / mean(V_total)
    abs_imbalances = [abs(b - s) for b, s in zip(buy_volumes, sell_volumes)]
    total_volumes = [b + s for b, s in zip(buy_volumes, sell_volumes)]

    mean_total = np.mean(total_volumes)
    if mean_total <= 0:
        return 0.0

    vpin = np.mean(abs_imbalances) / mean_total
    return float(np.clip(vpin, 0.0, 1.0))


# ============================================================
# Composite Factor Computation
# ============================================================


def compute_composite_score(
    obi: float,
    tfi: float,
    spread_zscore: float,
    microprice_dev: float,
    whale_imbalance: float,
    vpin: float,
    weights: Optional[Dict[str, float]] = None,
) -> float:
    """Combine all microstructure factors into a single directional score.

    Default weights emphasize OBI and TFI (most reliable at mid-frequency).
    VPIN is used as a contrarian risk signal (high VPIN = reduce confidence).
    Spread z-score is inverted (tight = favorable).

    Args:
        obi: Order book imbalance [-1, 1].
        tfi: Trade flow imbalance [-1, 1].
        spread_zscore: Spread z-score (negative = tight).
        microprice_dev: Microprice deviation in bps.
        whale_imbalance: Whale trade imbalance [-1, 1].
        vpin: VPIN [0, 1].
        weights: Optional custom weight dict.

    Returns:
        Composite score in [-1, 1] via tanh scaling.
    """
    w = weights or {
        "obi": 0.30,
        "tfi": 0.25,
        "microprice_dev": 0.20,
        "whale": 0.10,
        "spread_zscore": 0.10,
        "vpin": 0.05,
    }

    # Normalize microprice_dev: clip to [-10, 10] bps then scale to [-1, 1]
    mp_normalized = np.clip(microprice_dev / 10.0, -1.0, 1.0)

    # Compose: VPIN is contrarian (high = danger), spread_zscore inverted (tight = good)
    raw = (
        w.get("obi", 0.30) * obi
        + w.get("tfi", 0.25) * tfi
        + w.get("spread_zscore", 0.10) * (-spread_zscore / 3.0)  # normalize z-score to ~[-1,1]
        + w.get("microprice_dev", 0.20) * mp_normalized
        + w.get("whale", 0.10) * whale_imbalance
        + w.get("vpin", 0.05) * (-vpin * 2 + 1)  # vpin 0->+1, vpin 0.5->0, vpin 1->-1
    )

    # Tanh scaling to bound output in [-1, 1]
    return float(np.tanh(raw * 2.0))
