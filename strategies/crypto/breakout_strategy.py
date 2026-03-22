"""1-minute breakout strategy for crypto.

Detects breakouts from consolidation ranges with volume confirmation.
Uses ATR for dynamic stop placement and a trailing stop for profit taking.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Deque, Dict, List, Optional
from uuid import uuid4

import numpy as np

from core.enums import SignalDirection
from core.models import Bar, Signal
from strategies.base import StrategyBase, StrategyConfig
from strategies.registry import register_strategy


@register_strategy("crypto_breakout")
class BreakoutStrategy(StrategyBase):
    """1-minute candle breakout with volume confirmation and ATR stops."""

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

        # Parameters
        self.lookback = self.get_param("lookback_bars", 20)
        self.volume_multiplier = self.get_param("volume_multiplier", 2.0)
        self.atr_period = self.get_param("atr_period", 14)
        self.atr_stop_mult = self.get_param("atr_stop_multiplier", 2.0)
        self.atr_tp_mult = self.get_param("atr_tp_multiplier", 3.0)
        self.consolidation_atr_mult = self.get_param("consolidation_atr_mult", 0.5)

        # State per symbol
        self._bars: Dict[str, Deque[Bar]] = {}
        self._atr: Dict[str, float] = {}
        self._avg_volume: Dict[str, float] = {}
        self._range_high: Dict[str, float] = {}
        self._range_low: Dict[str, float] = {}

    def on_bar(self, symbol: str, bar: Bar) -> None:
        """Process new 1-minute bar."""
        if symbol not in self._bars:
            self._bars[symbol] = deque(maxlen=max(self.lookback, self.atr_period) + 5)
        self._bars[symbol].append(bar)

        bars = self._bars[symbol]
        if len(bars) < self.lookback:
            return

        # Compute ATR
        closes = [b.close for b in bars]
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]

        trs = []
        for i in range(1, len(bars)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)
        if trs:
            self._atr[symbol] = np.mean(trs[-self.atr_period:])

        # Average volume
        volumes = [b.volume for b in bars]
        self._avg_volume[symbol] = np.mean(volumes[-self.lookback:])

        # Compute consolidation range (last N bars)
        recent_highs = highs[-self.lookback:]
        recent_lows = lows[-self.lookback:]
        self._range_high[symbol] = max(recent_highs)
        self._range_low[symbol] = min(recent_lows)

    def generate_signals(self) -> List[Signal]:
        """Detect breakouts from consolidation with volume confirmation."""
        signals: List[Signal] = []
        now = datetime.utcnow()

        for symbol in self.symbols:
            bars = self._bars.get(symbol)
            if not bars or len(bars) < self.lookback:
                continue

            current = bars[-1]
            atr = self._atr.get(symbol, 0)
            avg_vol = self._avg_volume.get(symbol, 0)
            range_high = self._range_high.get(symbol, 0)
            range_low = self._range_low.get(symbol, 0)

            if atr == 0 or avg_vol == 0:
                continue

            # Check if we were in consolidation (range < threshold * ATR * lookback)
            range_size = range_high - range_low
            is_consolidation = range_size < self.consolidation_atr_mult * atr * np.sqrt(self.lookback)

            # Volume surge check
            has_volume = current.volume > self.volume_multiplier * avg_vol

            direction = None
            strength = 0.0

            # Bullish breakout
            if (current.close > range_high
                    and has_volume
                    and is_consolidation
                    and self.is_flat(symbol)):
                direction = SignalDirection.LONG
                breakout_magnitude = (current.close - range_high) / atr
                strength = min(breakout_magnitude * 0.5, 1.0)

            # Bearish breakout
            elif (current.close < range_low
                    and has_volume
                    and is_consolidation
                    and self.is_flat(symbol)):
                direction = SignalDirection.SHORT
                breakout_magnitude = (range_low - current.close) / atr
                strength = min(breakout_magnitude * 0.5, 1.0)

            if direction is not None:
                if direction == SignalDirection.LONG:
                    sl = current.close - self.atr_stop_mult * atr
                    tp = current.close + self.atr_tp_mult * atr
                else:
                    sl = current.close + self.atr_stop_mult * atr
                    tp = current.close - self.atr_tp_mult * atr

                signals.append(Signal(
                    signal_id=uuid4(),
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=direction,
                    strength=strength,
                    timestamp=now,
                    target_price=current.close,
                    take_profit=tp,
                    stop_loss=sl,
                    metadata={
                        "atr": atr,
                        "volume_ratio": current.volume / avg_vol if avg_vol else 0,
                        "range_high": range_high,
                        "range_low": range_low,
                        "range_size_atr": range_size / atr if atr else 0,
                    },
                ))

        return signals
