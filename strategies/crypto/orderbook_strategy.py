"""Orderbook trading strategy for crypto.

Detects liquidity imbalances, absorption events, and large walls in the
L2 order book to predict short-term price direction. Targets 10-60 second
holding periods.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Deque, Dict, List, Optional, Tuple
from uuid import uuid4

import numpy as np

from core.enums import SignalDirection
from core.models import Bar, OrderBookSnapshot, Signal
from data.features.microstructure import (
    compute_depth_imbalance,
    compute_microprice,
    compute_orderbook_imbalance,
    compute_spread_features,
)
from strategies.base import StrategyBase, StrategyConfig
from strategies.registry import register_strategy


@register_strategy("crypto_orderbook")
class OrderbookStrategy(StrategyBase):
    """L2 orderbook imbalance and liquidity wall detection."""

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

        # Parameters
        self.depth_levels = self.get_param("depth_levels", 10)
        self.imbalance_threshold = self.get_param("imbalance_threshold", 0.4)
        self.wall_size_multiplier = self.get_param("wall_size_multiplier", 5.0)
        self.spread_max_bps = self.get_param("spread_max_bps", 10)
        self.signal_decay_seconds = self.get_param("signal_decay_seconds", 10)
        self.min_trade_interval_sec = self.get_param("min_trade_interval_sec", 15)

        # State
        self._orderbooks: Dict[str, Deque[OrderBookSnapshot]] = {}
        self._imbalance_history: Dict[str, Deque[float]] = {}
        self._microprice_history: Dict[str, Deque[float]] = {}
        self._last_signal_time: Dict[str, datetime] = {}

    def on_orderbook(self, symbol: str, orderbook: OrderBookSnapshot) -> None:
        """Process orderbook update."""
        if symbol not in self._orderbooks:
            self._orderbooks[symbol] = deque(maxlen=100)
            self._imbalance_history[symbol] = deque(maxlen=200)
            self._microprice_history[symbol] = deque(maxlen=200)

        self._orderbooks[symbol].append(orderbook)
        self._microprice_history[symbol].append(orderbook.microprice)

        # Compute multi-level imbalance
        bids = [(l.price, l.quantity) for l in orderbook.bids[:self.depth_levels]]
        asks = [(l.price, l.quantity) for l in orderbook.asks[:self.depth_levels]]
        imbalance = compute_depth_imbalance(bids, asks, self.depth_levels)
        self._imbalance_history[symbol].append(imbalance)

    def generate_signals(self) -> List[Signal]:
        """Generate signals from orderbook imbalance patterns."""
        signals: List[Signal] = []
        now = datetime.utcnow()

        for symbol in self.symbols:
            ob_history = self._orderbooks.get(symbol)
            if not ob_history or len(ob_history) < 5:
                continue

            # Cooldown check
            last_sig = self._last_signal_time.get(symbol)
            if last_sig and (now - last_sig).total_seconds() < self.min_trade_interval_sec:
                continue

            ob = ob_history[-1]
            imb_hist = list(self._imbalance_history[symbol])
            mp_hist = list(self._microprice_history[symbol])

            # Spread filter: only trade tight markets
            spread_info = compute_spread_features(ob.best_bid, ob.best_ask)
            if spread_info["spread_bps"] > self.spread_max_bps:
                continue

            # Signal conditions
            current_imbalance = imb_hist[-1]
            avg_imbalance = np.mean(imb_hist[-20:]) if len(imb_hist) >= 20 else 0

            # Detect persistent imbalance (not just a spike)
            recent_imb = imb_hist[-5:]
            consistent_buy = all(i > self.imbalance_threshold * 0.5 for i in recent_imb)
            consistent_sell = all(i < -self.imbalance_threshold * 0.5 for i in recent_imb)

            # Detect walls
            wall_direction = self._detect_walls(ob)

            # Microprice trend
            mp_trend = 0
            if len(mp_hist) >= 10:
                mp_trend = (mp_hist[-1] - mp_hist[-10]) / mp_hist[-10] * 10000  # bps

            direction = None
            strength = 0.0

            # Strong buy: consistent buy imbalance + rising microprice + no sell wall
            if (consistent_buy
                    and current_imbalance > self.imbalance_threshold
                    and mp_trend > 0
                    and wall_direction != "sell"
                    and self.is_flat(symbol)):
                direction = SignalDirection.LONG
                strength = min(abs(current_imbalance), 1.0) * 0.8

            # Strong sell: consistent sell imbalance + falling microprice + no buy wall
            elif (consistent_sell
                    and current_imbalance < -self.imbalance_threshold
                    and mp_trend < 0
                    and wall_direction != "buy"
                    and self.is_flat(symbol)):
                direction = SignalDirection.SHORT
                strength = min(abs(current_imbalance), 1.0) * 0.8

            # Exit on imbalance reversal
            elif not self.is_flat(symbol):
                pos = self.get_position(symbol)
                if pos > 0 and current_imbalance < 0:
                    direction = SignalDirection.FLAT
                    strength = 0.7
                elif pos < 0 and current_imbalance > 0:
                    direction = SignalDirection.FLAT
                    strength = 0.7

            if direction is not None:
                signals.append(Signal(
                    signal_id=uuid4(),
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=direction,
                    strength=strength,
                    timestamp=now,
                    target_price=ob.microprice,
                    metadata={
                        "imbalance": current_imbalance,
                        "avg_imbalance": avg_imbalance,
                        "microprice": ob.microprice,
                        "spread_bps": spread_info["spread_bps"],
                        "mp_trend_bps": mp_trend,
                        "wall": wall_direction,
                    },
                ))
                self._last_signal_time[symbol] = now

        return signals

    def _detect_walls(self, ob: OrderBookSnapshot) -> Optional[str]:
        """Detect large liquidity walls in the orderbook.

        Returns "buy" if large bid wall, "sell" if large ask wall, None otherwise.
        """
        if len(ob.bids) < 5 or len(ob.asks) < 5:
            return None

        bid_sizes = [l.quantity for l in ob.bids[:self.depth_levels]]
        ask_sizes = [l.quantity for l in ob.asks[:self.depth_levels]]

        avg_bid = np.mean(bid_sizes) if bid_sizes else 0
        avg_ask = np.mean(ask_sizes) if ask_sizes else 0

        max_bid = max(bid_sizes) if bid_sizes else 0
        max_ask = max(ask_sizes) if ask_sizes else 0

        if avg_bid > 0 and max_bid > self.wall_size_multiplier * avg_bid:
            return "buy"
        if avg_ask > 0 and max_ask > self.wall_size_multiplier * avg_ask:
            return "sell"
        return None
