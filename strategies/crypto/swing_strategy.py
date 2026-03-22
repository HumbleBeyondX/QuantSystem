"""Multi-timeframe swing trading strategy for crypto.

Uses regime detection (trending vs ranging) across multiple timeframes
to capture intermediate-term trends. 1H/4H for direction, 15M for entry.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Deque, Dict, List
from uuid import uuid4

import numpy as np

from core.enums import SignalDirection
from core.models import Bar, Signal
from strategies.base import StrategyBase, StrategyConfig
from strategies.registry import register_strategy


@register_strategy("crypto_swing")
class SwingStrategy(StrategyBase):
    """Multi-timeframe trend following with regime detection."""

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

        # Parameters
        self.ema_fast = self.get_param("ema_fast", 21)
        self.ema_slow = self.get_param("ema_slow", 55)
        self.ema_trend = self.get_param("ema_trend", 200)
        self.adx_threshold = self.get_param("adx_threshold", 25)
        self.atr_period = self.get_param("atr_period", 14)
        self.atr_stop_mult = self.get_param("atr_stop_multiplier", 2.5)
        self.atr_tp_mult = self.get_param("atr_tp_multiplier", 4.0)
        self.rsi_filter_low = self.get_param("rsi_filter_low", 35)
        self.rsi_filter_high = self.get_param("rsi_filter_high", 65)
        self.trailing_stop_atr = self.get_param("trailing_stop_atr", 2.0)

        # State per symbol
        self._bars: Dict[str, Deque[Bar]] = {}
        self._ema_f: Dict[str, float] = {}
        self._ema_s: Dict[str, float] = {}
        self._ema_t: Dict[str, float] = {}
        self._highest_since_entry: Dict[str, float] = {}
        self._lowest_since_entry: Dict[str, float] = {}

    def on_bar(self, symbol: str, bar: Bar) -> None:
        """Process bar and update EMAs."""
        super().on_bar(symbol, bar)

        if symbol not in self._bars:
            self._bars[symbol] = deque(maxlen=250)
            self._ema_f[symbol] = bar.close
            self._ema_s[symbol] = bar.close
            self._ema_t[symbol] = bar.close

        self._bars[symbol].append(bar)
        price = bar.close

        # Update EMAs
        af = 2 / (self.ema_fast + 1)
        a_s = 2 / (self.ema_slow + 1)
        at = 2 / (self.ema_trend + 1)
        self._ema_f[symbol] = af * price + (1 - af) * self._ema_f[symbol]
        self._ema_s[symbol] = a_s * price + (1 - a_s) * self._ema_s[symbol]
        self._ema_t[symbol] = at * price + (1 - at) * self._ema_t[symbol]

        # Track trailing stop levels
        if not self.is_flat(symbol):
            pos = self.get_position(symbol)
            if pos > 0:
                self._highest_since_entry[symbol] = max(
                    self._highest_since_entry.get(symbol, price), price
                )
            elif pos < 0:
                self._lowest_since_entry[symbol] = min(
                    self._lowest_since_entry.get(symbol, price), price
                )

    def generate_signals(self) -> List[Signal]:
        """Generate swing trading signals."""
        signals: List[Signal] = []
        now = datetime.utcnow()

        for symbol in self.symbols:
            bars = self._bars.get(symbol)
            if not bars or len(bars) < self.ema_trend:
                continue

            price = bars[-1].close
            ema_f = self._ema_f[symbol]
            ema_s = self._ema_s[symbol]
            ema_t = self._ema_t[symbol]

            # Compute ATR
            atr = self._compute_atr(bars)
            if atr == 0:
                continue

            # Compute RSI
            rsi = self._compute_rsi(bars)

            # Trend regime: price above/below 200 EMA
            is_uptrend = price > ema_t and ema_f > ema_s
            is_downtrend = price < ema_t and ema_f < ema_s

            direction = None
            strength = 0.0

            if self.is_flat(symbol):
                # Entry: EMA crossover aligned with trend
                if is_uptrend and rsi < self.rsi_filter_high:
                    # Pullback entry in uptrend
                    if price > ema_f and ema_f > ema_s > ema_t:
                        direction = SignalDirection.LONG
                        # Strength based on trend alignment
                        trend_strength = (price - ema_t) / atr
                        strength = min(trend_strength * 0.2, 1.0)

                elif is_downtrend and rsi > self.rsi_filter_low:
                    if price < ema_f and ema_f < ema_s < ema_t:
                        direction = SignalDirection.SHORT
                        trend_strength = (ema_t - price) / atr
                        strength = min(trend_strength * 0.2, 1.0)

            else:
                # Exit: trailing stop or trend reversal
                pos = self.get_position(symbol)

                if pos > 0:
                    high = self._highest_since_entry.get(symbol, price)
                    trailing_stop = high - self.trailing_stop_atr * atr
                    if price < trailing_stop or ema_f < ema_s:
                        direction = SignalDirection.FLAT
                        strength = 0.9

                elif pos < 0:
                    low = self._lowest_since_entry.get(symbol, price)
                    trailing_stop = low + self.trailing_stop_atr * atr
                    if price > trailing_stop or ema_f > ema_s:
                        direction = SignalDirection.FLAT
                        strength = 0.9

            if direction is not None:
                sl = tp = None
                if direction == SignalDirection.LONG:
                    sl = price - self.atr_stop_mult * atr
                    tp = price + self.atr_tp_mult * atr
                    self._highest_since_entry[symbol] = price
                elif direction == SignalDirection.SHORT:
                    sl = price + self.atr_stop_mult * atr
                    tp = price - self.atr_tp_mult * atr
                    self._lowest_since_entry[symbol] = price

                signals.append(Signal(
                    signal_id=uuid4(),
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=direction,
                    strength=strength,
                    timestamp=now,
                    target_price=price,
                    stop_loss=sl,
                    take_profit=tp,
                    metadata={
                        "ema_fast": ema_f, "ema_slow": ema_s, "ema_trend": ema_t,
                        "atr": atr, "rsi": rsi,
                    },
                ))

        return signals

    def _compute_atr(self, bars: Deque[Bar], period: int = None) -> float:
        period = period or self.atr_period
        if len(bars) < period + 1:
            return 0.0
        trs = []
        bar_list = list(bars)
        for i in range(-period, 0):
            tr = max(
                bar_list[i].high - bar_list[i].low,
                abs(bar_list[i].high - bar_list[i - 1].close),
                abs(bar_list[i].low - bar_list[i - 1].close),
            )
            trs.append(tr)
        return float(np.mean(trs))

    def _compute_rsi(self, bars: Deque[Bar], period: int = 14) -> float:
        if len(bars) < period + 1:
            return 50.0
        closes = [b.close for b in list(bars)[-(period + 1):]]
        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [d for d in deltas if d > 0]
        losses = [-d for d in deltas if d < 0]
        avg_gain = np.mean(gains) if gains else 0
        avg_loss = np.mean(losses) if losses else 0
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))
