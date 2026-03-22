"""Microstructure features for high-frequency and orderbook-based strategies.

Includes Order Flow Imbalance (OFI), VPIN, microprice, orderbook slope,
and other market microstructure indicators critical for crypto scalping
and orderbook strategies.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from core.logging import get_logger

logger = get_logger(__name__)


# ============================================================
# Order Book Features
# ============================================================


def compute_microprice(
    best_bid: float, best_ask: float,
    bid_qty: float, ask_qty: float,
) -> float:
    """Volume-weighted microprice (better fair value estimator than mid).

    microprice = (bid * ask_qty + ask * bid_qty) / (bid_qty + ask_qty)
    """
    total = bid_qty + ask_qty
    if total == 0:
        return (best_bid + best_ask) / 2
    return (best_bid * ask_qty + best_ask * bid_qty) / total


def compute_orderbook_imbalance(bid_qty: float, ask_qty: float) -> float:
    """Order book imbalance at top of book.

    Returns value in [-1, 1]: positive = buy pressure, negative = sell pressure.
    """
    total = bid_qty + ask_qty
    if total == 0:
        return 0.0
    return (bid_qty - ask_qty) / total


def compute_depth_imbalance(
    bids: List[tuple], asks: List[tuple], levels: int = 5
) -> float:
    """Multi-level depth imbalance.

    Args:
        bids: List of (price, qty) tuples, sorted descending by price
        asks: List of (price, qty) tuples, sorted ascending by price
        levels: Number of levels to consider
    """
    bid_depth = sum(qty for _, qty in bids[:levels])
    ask_depth = sum(qty for _, qty in asks[:levels])
    total = bid_depth + ask_depth
    if total == 0:
        return 0.0
    return (bid_depth - ask_depth) / total


def compute_orderbook_slope(
    levels: List[tuple], side: str = "bid"
) -> float:
    """Orderbook slope: how quickly depth accumulates away from best price.

    Higher slope = more resilient to market impact.
    """
    if len(levels) < 2:
        return 0.0

    best_price = levels[0][0]
    if best_price == 0:
        return 0.0

    # Cumulative quantity vs price distance from best
    cum_qty = 0.0
    weighted_dist = 0.0
    for price, qty in levels:
        dist = abs(price - best_price) / best_price
        cum_qty += qty
        weighted_dist += dist * qty

    if cum_qty == 0:
        return 0.0
    return cum_qty / (weighted_dist / cum_qty) if weighted_dist > 0 else 0.0


def compute_spread_features(
    best_bid: float, best_ask: float
) -> Dict[str, float]:
    """Compute spread-related features."""
    mid = (best_bid + best_ask) / 2
    spread = best_ask - best_bid
    return {
        "spread_abs": spread,
        "spread_bps": (spread / mid * 10000) if mid > 0 else 0.0,
        "mid_price": mid,
    }


# ============================================================
# Order Flow Features (from trade tape)
# ============================================================


def compute_ofi(
    df: pd.DataFrame, window: int = 50
) -> pd.Series:
    """Order Flow Imbalance (OFI).

    Measures net signed order flow: sum of (trade_size * direction).
    Requires columns: price, amount, side (or inferred from price changes).
    """
    if "side" in df.columns:
        sign = df["side"].map({"BUY": 1, "buy": 1, "SELL": -1, "sell": -1}).fillna(0)
    else:
        # Infer from tick rule: price up = buy, price down = sell
        sign = np.sign(df["price"].diff()).fillna(0)

    qty = df.get("amount", df.get("volume", pd.Series(1.0, index=df.index)))
    signed_flow = sign * qty
    return signed_flow.rolling(window=window).sum()


def compute_trade_flow_imbalance(
    df: pd.DataFrame, window: int = 100
) -> pd.Series:
    """Trade flow imbalance: (buy_volume - sell_volume) / total_volume over window."""
    if "side" in df.columns:
        is_buy = df["side"].isin(["BUY", "buy"])
    else:
        is_buy = df["price"].diff() > 0

    qty = df.get("amount", df.get("volume", pd.Series(1.0, index=df.index)))
    buy_vol = (qty * is_buy).rolling(window=window).sum()
    sell_vol = (qty * ~is_buy).rolling(window=window).sum()
    total = buy_vol + sell_vol
    return ((buy_vol - sell_vol) / total.replace(0, np.nan)).fillna(0)


def compute_vpin(
    df: pd.DataFrame, bucket_size: float = 1000.0, n_buckets: int = 50
) -> pd.Series:
    """Volume-synchronized Probability of Informed Trading (VPIN).

    Approximation using bulk volume classification.
    """
    if "close" not in df.columns:
        return pd.Series(np.nan, index=df.index)

    # Classify each bar's volume as buy/sell using close vs range
    if df["high"].equals(df["low"]):
        buy_pct = pd.Series(0.5, index=df.index)
    else:
        buy_pct = (df["close"] - df["low"]) / (df["high"] - df["low"]).replace(0, np.nan)
        buy_pct = buy_pct.fillna(0.5)

    buy_vol = buy_pct * df["volume"]
    sell_vol = (1 - buy_pct) * df["volume"]
    abs_imbalance = abs(buy_vol - sell_vol)

    # Rolling VPIN over n_buckets bars
    total_vol = df["volume"].rolling(window=n_buckets).sum()
    sum_imbalance = abs_imbalance.rolling(window=n_buckets).sum()
    vpin = sum_imbalance / total_vol.replace(0, np.nan)
    return vpin.fillna(0)


def compute_trade_intensity(
    df: pd.DataFrame, window: int = 60
) -> pd.Series:
    """Trade intensity: number of trades per unit time (rolling count)."""
    return pd.Series(1, index=df.index).rolling(window=window).sum()


def compute_kyle_lambda(
    df: pd.DataFrame, window: int = 100
) -> pd.Series:
    """Kyle's Lambda: price impact coefficient.

    Estimated as regression slope of price change on signed volume.
    """
    price_change = df["close"].diff() if "close" in df.columns else df["price"].diff()
    qty = df.get("amount", df.get("volume", pd.Series(1.0, index=df.index)))

    if "side" in df.columns:
        sign = df["side"].map({"BUY": 1, "buy": 1, "SELL": -1, "sell": -1}).fillna(0)
    else:
        sign = np.sign(price_change).fillna(0)

    signed_vol = sign * qty

    # Rolling regression slope
    def _rolling_slope(y: pd.Series, x: pd.Series, w: int) -> pd.Series:
        xy = (x * y).rolling(window=w).mean()
        x2 = (x * x).rolling(window=w).mean()
        xm = x.rolling(window=w).mean()
        ym = y.rolling(window=w).mean()
        cov = xy - xm * ym
        var = x2 - xm * xm
        return cov / var.replace(0, np.nan)

    return _rolling_slope(price_change, signed_vol, window).fillna(0)


# ============================================================
# Feature Registry
# ============================================================

MICROSTRUCTURE_FEATURES: Dict[str, tuple] = {
    "ofi_50": (lambda df: compute_ofi(df, 50), "orderflow", 50),
    "ofi_200": (lambda df: compute_ofi(df, 200), "orderflow", 200),
    "trade_flow_imbalance": (lambda df: compute_trade_flow_imbalance(df, 100), "orderflow", 100),
    "vpin": (lambda df: compute_vpin(df), "orderflow", 50),
    "trade_intensity_60": (lambda df: compute_trade_intensity(df, 60), "orderflow", 60),
    "kyle_lambda": (lambda df: compute_kyle_lambda(df, 100), "orderflow", 100),
}


def register_microstructure_features(store) -> None:
    """Register all microstructure features with a FeatureStore instance."""
    for name, (func, category, lookback) in MICROSTRUCTURE_FEATURES.items():
        store.register(
            name=name,
            func=func,
            category=f"microstructure_{category}",
            lookback=lookback,
        )
