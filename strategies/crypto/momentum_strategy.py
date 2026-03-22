"""BTC Momentum Strategy: trend-following with multi-signal confirmation.

Classic momentum strategy optimized for BTC on daily/4H timeframes.
Uses EMA crossovers, ROC (Rate of Change), RSI momentum, and ADX trend filter
with ATR-based dynamic stops.

Designed for medium-term holding (days to weeks), captures major BTC trends
like the 2023-2024 rally and 2025 cycle.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List
from uuid import uuid4

import numpy as np

from core.enums import SignalDirection
from core.models import Bar, Signal, TargetPosition
from strategies.base import StrategyBase, StrategyConfig
from strategies.registry import register_strategy


@register_strategy("crypto_momentum")
class MomentumStrategy(StrategyBase):
    """Trend-following momentum strategy for BTC.

    Entry: EMA crossover + ROC confirmation + ADX trend filter
    Exit: Trailing stop (ATR-based) or EMA cross reversal
    Position sizing: dollar-based, scaled by trend strength
    """

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

        # EMA periods
        self.ema_fast_period = self.get_param("ema_fast", 20)
        self.ema_slow_period = self.get_param("ema_slow", 50)
        self.ema_trend_period = self.get_param("ema_trend", 200)

        # Momentum parameters
        self.roc_period = self.get_param("roc_period", 20)
        self.rsi_period = self.get_param("rsi_period", 14)
        self.adx_period = self.get_param("adx_period", 14)
        self.adx_threshold = self.get_param("adx_threshold", 20)

        # Entry filters
        self.rsi_long_max = self.get_param("rsi_long_max", 75)
        self.rsi_long_min = self.get_param("rsi_long_min", 35)
        self.rsi_short_max = self.get_param("rsi_short_max", 65)
        self.rsi_short_min = self.get_param("rsi_short_min", 25)
        self.roc_threshold = self.get_param("roc_threshold", 0.02)  # 2% momentum

        # Risk management
        self.atr_period = self.get_param("atr_period", 14)
        self.atr_stop_mult = self.get_param("atr_stop_mult", 3.0)
        self.atr_tp_mult = self.get_param("atr_tp_mult", 5.0)
        self.trailing_stop_atr = self.get_param("trailing_stop_atr", 2.5)
        self.max_position_usd = self.get_param("max_position_usd", 50000)

        # State
        self._bars: Dict[str, Deque[Bar]] = {}
        self._ema_fast: Dict[str, float] = {}
        self._ema_slow: Dict[str, float] = {}
        self._ema_trend: Dict[str, float] = {}
        self._highest_since_entry: Dict[str, float] = {}
        self._lowest_since_entry: Dict[str, float] = {}
        self._entry_price: Dict[str, float] = {}
        self._current_time: datetime = datetime.utcnow()

    def on_bar(self, symbol: str, bar: Bar) -> None:
        """Process each bar: update EMAs and state."""
        super().on_bar(symbol, bar)
        self._current_time = bar.timestamp
        price = bar.close

        if symbol not in self._bars:
            self._bars[symbol] = deque(maxlen=300)
            self._ema_fast[symbol] = price
            self._ema_slow[symbol] = price
            self._ema_trend[symbol] = price

        self._bars[symbol].append(bar)

        # Update EMAs
        af = 2 / (self.ema_fast_period + 1)
        a_s = 2 / (self.ema_slow_period + 1)
        at = 2 / (self.ema_trend_period + 1)
        self._ema_fast[symbol] = af * price + (1 - af) * self._ema_fast[symbol]
        self._ema_slow[symbol] = a_s * price + (1 - a_s) * self._ema_slow[symbol]
        self._ema_trend[symbol] = at * price + (1 - at) * self._ema_trend[symbol]

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

    def calculate_target_positions(self) -> List[TargetPosition]:
        """Dollar-based position sizing for momentum."""
        signals = self.generate_signals()
        self._signals.extend(signals)
        self._metrics["signals_generated"] += len(signals)

        targets = []
        for signal in signals:
            symbol = signal.symbol
            bars = self._bars.get(symbol)
            if not bars:
                continue
            price = bars[-1].close
            if price <= 0:
                continue

            position_usd = self.max_position_usd * signal.strength
            qty_in_asset = position_usd / price

            if signal.direction == SignalDirection.LONG:
                target_qty = qty_in_asset
            elif signal.direction == SignalDirection.SHORT:
                target_qty = -qty_in_asset
            else:
                target_qty = 0.0

            targets.append(TargetPosition(
                symbol=symbol,
                strategy_id=self.strategy_id,
                target_quantity=target_qty,
                target_weight=signal.strength,
                urgency=0.5,
                timestamp=signal.timestamp,
            ))

        return targets

    def generate_signals(self) -> List[Signal]:
        """Generate momentum signals with multi-indicator confirmation."""
        signals: List[Signal] = []
        now = self._current_time

        for symbol in self.symbols:
            bars = self._bars.get(symbol)
            if not bars or len(bars) < max(self.ema_trend_period, self.roc_period + 1):
                continue

            price = bars[-1].close
            ema_f = self._ema_fast[symbol]
            ema_s = self._ema_slow[symbol]
            ema_t = self._ema_trend[symbol]

            # Compute indicators
            atr = self._compute_atr(bars)
            rsi = self._compute_rsi(bars)
            roc = self._compute_roc(bars)
            adx = self._compute_adx(bars)

            if atr == 0:
                continue

            direction = None
            strength = 0.0

            if self.is_flat(symbol):
                # === LONG ENTRY ===
                # Conditions: EMA fast > slow, price above trend EMA,
                # positive momentum (ROC), RSI not overbought, ADX trending
                ema_bullish = ema_f > ema_s and price > ema_t
                roc_bullish = roc > self.roc_threshold
                rsi_valid = self.rsi_long_min < rsi < self.rsi_long_max
                adx_trending = adx > self.adx_threshold

                if ema_bullish and roc_bullish and rsi_valid and adx_trending:
                    direction = SignalDirection.LONG
                    # Strength from trend alignment and momentum
                    trend_strength = min((price - ema_t) / (atr * 5), 1.0)
                    momentum_strength = min(abs(roc) / 0.10, 1.0)  # normalize to 10%
                    strength = max(0.3, min((trend_strength + momentum_strength) / 2, 1.0))

                # === SHORT ENTRY ===
                ema_bearish = ema_f < ema_s and price < ema_t
                roc_bearish = roc < -self.roc_threshold
                rsi_short_valid = self.rsi_short_min < rsi < self.rsi_short_max

                if ema_bearish and roc_bearish and rsi_short_valid and adx_trending:
                    direction = SignalDirection.SHORT
                    trend_strength = min((ema_t - price) / (atr * 5), 1.0)
                    momentum_strength = min(abs(roc) / 0.10, 1.0)
                    strength = max(0.3, min((trend_strength + momentum_strength) / 2, 1.0))

            else:
                # === EXIT SIGNALS ===
                pos = self.get_position(symbol)

                if pos > 0:
                    # Long exit: trailing stop or trend reversal
                    high = self._highest_since_entry.get(symbol, price)
                    trailing_stop = high - self.trailing_stop_atr * atr

                    # Exit conditions
                    hit_trailing = price < trailing_stop
                    ema_cross_down = ema_f < ema_s
                    rsi_extreme = rsi > 85

                    if hit_trailing or ema_cross_down or rsi_extreme:
                        direction = SignalDirection.FLAT
                        strength = 1.0

                elif pos < 0:
                    # Short exit
                    low = self._lowest_since_entry.get(symbol, price)
                    trailing_stop = low + self.trailing_stop_atr * atr

                    hit_trailing = price > trailing_stop
                    ema_cross_up = ema_f > ema_s
                    rsi_extreme = rsi < 15

                    if hit_trailing or ema_cross_up or rsi_extreme:
                        direction = SignalDirection.FLAT
                        strength = 1.0

            if direction is not None:
                sl = tp = None
                if direction == SignalDirection.LONG:
                    sl = price - self.atr_stop_mult * atr
                    tp = price + self.atr_tp_mult * atr
                    self._highest_since_entry[symbol] = price
                    self._entry_price[symbol] = price
                elif direction == SignalDirection.SHORT:
                    sl = price + self.atr_stop_mult * atr
                    tp = price - self.atr_tp_mult * atr
                    self._lowest_since_entry[symbol] = price
                    self._entry_price[symbol] = price
                else:
                    # Clear tracking on exit
                    self._highest_since_entry.pop(symbol, None)
                    self._lowest_since_entry.pop(symbol, None)
                    self._entry_price.pop(symbol, None)

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
                        "ema_fast": round(ema_f, 2),
                        "ema_slow": round(ema_s, 2),
                        "ema_trend": round(ema_t, 2),
                        "roc": round(roc, 4),
                        "rsi": round(rsi, 2),
                        "adx": round(adx, 2),
                        "atr": round(atr, 2),
                    },
                ))

        return signals

    # ---------------------------------------------------------------
    # Technical indicator helpers
    # ---------------------------------------------------------------

    def _compute_atr(self, bars: Deque[Bar]) -> float:
        period = self.atr_period
        if len(bars) < period + 1:
            return 0.0
        bar_list = list(bars)
        trs = []
        for i in range(-period, 0):
            tr = max(
                bar_list[i].high - bar_list[i].low,
                abs(bar_list[i].high - bar_list[i - 1].close),
                abs(bar_list[i].low - bar_list[i - 1].close),
            )
            trs.append(tr)
        return float(np.mean(trs))

    def _compute_rsi(self, bars: Deque[Bar]) -> float:
        period = self.rsi_period
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
        return float(100 - (100 / (1 + rs)))

    def _compute_roc(self, bars: Deque[Bar]) -> float:
        """Rate of Change: (price - price_n) / price_n."""
        if len(bars) < self.roc_period + 1:
            return 0.0
        bar_list = list(bars)
        current = bar_list[-1].close
        past = bar_list[-(self.roc_period + 1)].close
        if past == 0:
            return 0.0
        return (current - past) / past

    def _compute_adx(self, bars: Deque[Bar]) -> float:
        """Simplified ADX approximation using directional movement."""
        period = self.adx_period
        if len(bars) < period * 2:
            return 25.0  # Default neutral value

        bar_list = list(bars)
        plus_dm = []
        minus_dm = []
        trs = []

        for i in range(-period * 2 + 1, 0):
            high_diff = bar_list[i].high - bar_list[i - 1].high
            low_diff = bar_list[i - 1].low - bar_list[i].low
            tr = max(
                bar_list[i].high - bar_list[i].low,
                abs(bar_list[i].high - bar_list[i - 1].close),
                abs(bar_list[i].low - bar_list[i - 1].close),
            )
            trs.append(tr)
            plus_dm.append(max(high_diff, 0) if high_diff > low_diff else 0)
            minus_dm.append(max(low_diff, 0) if low_diff > high_diff else 0)

        # Smooth
        atr_val = np.mean(trs[-period:])
        if atr_val == 0:
            return 0.0

        plus_di = 100 * np.mean(plus_dm[-period:]) / atr_val
        minus_di = 100 * np.mean(minus_dm[-period:]) / atr_val

        di_sum = plus_di + minus_di
        if di_sum == 0:
            return 0.0

        dx = 100 * abs(plus_di - minus_di) / di_sum
        return float(dx)
