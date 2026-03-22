"""Market timing strategy using regime detection.

Uses Hidden Markov Model (HMM) to classify market regimes (bull, bear, neutral)
and adjusts equity exposure accordingly. Designed for long-term allocation.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Deque, Dict, List
from uuid import uuid4

import numpy as np

from core.enums import SignalDirection
from core.models import Bar, Signal, TargetPosition
from strategies.base import StrategyBase, StrategyConfig
from strategies.registry import register_strategy


class RegimeDetector:
    """Simple regime detection using volatility and trend indicators.

    In Phase 4, this will be replaced with a proper HMM (hmmlearn).
    """

    def __init__(
        self,
        vol_lookback: int = 21,
        trend_lookback: int = 200,
        vol_threshold_high: float = 0.25,
        vol_threshold_low: float = 0.12,
    ) -> None:
        self.vol_lookback = vol_lookback
        self.trend_lookback = trend_lookback
        self.vol_threshold_high = vol_threshold_high
        self.vol_threshold_low = vol_threshold_low

    def detect(self, prices: list) -> str:
        """Classify current regime.

        Returns: "bull", "bear", or "neutral"
        """
        if len(prices) < self.trend_lookback:
            return "neutral"

        prices_arr = np.array(prices)
        returns = np.diff(np.log(prices_arr))

        # Trend: price relative to SMA
        sma = np.mean(prices_arr[-self.trend_lookback:])
        trend_up = prices_arr[-1] > sma
        trend_strength = (prices_arr[-1] - sma) / sma

        # Volatility regime
        recent_vol = np.std(returns[-self.vol_lookback:]) * np.sqrt(252)
        is_high_vol = recent_vol > self.vol_threshold_high
        is_low_vol = recent_vol < self.vol_threshold_low

        # Regime classification
        if trend_up and not is_high_vol:
            return "bull"
        elif not trend_up and is_high_vol:
            return "bear"
        elif trend_up and is_high_vol:
            return "neutral"  # Trend up but volatile: cautious
        else:
            return "neutral"


@register_strategy("equity_timing")
class TimingStrategy(StrategyBase):
    """Market regime-based timing for long-term equity allocation."""

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

        # Parameters
        self.bull_allocation = self.get_param("bull_allocation", 1.0)
        self.neutral_allocation = self.get_param("neutral_allocation", 0.5)
        self.bear_allocation = self.get_param("bear_allocation", 0.0)
        self.rebalance_days = self.get_param("rebalance_days", 5)
        self.vol_lookback = self.get_param("vol_lookback", 21)
        self.trend_lookback = self.get_param("trend_lookback", 200)

        # State
        self._prices: Dict[str, Deque[float]] = {}
        self._regime: Dict[str, str] = {}
        self._detector = RegimeDetector(
            vol_lookback=self.vol_lookback,
            trend_lookback=self.trend_lookback,
        )
        self._bar_count = 0

    def on_bar(self, symbol: str, bar: Bar) -> None:
        super().on_bar(symbol, bar)
        if symbol not in self._prices:
            self._prices[symbol] = deque(maxlen=300)
        self._prices[symbol].append(bar.close)
        self._bar_count += 1

        # Update regime
        prices_list = list(self._prices[symbol])
        self._regime[symbol] = self._detector.detect(prices_list)

    def generate_signals(self) -> List[Signal]:
        """Generate allocation signals based on regime."""
        if self._bar_count % self.rebalance_days != 0:
            return []

        signals: List[Signal] = []
        now = datetime.utcnow()

        for symbol in self.symbols:
            regime = self._regime.get(symbol, "neutral")
            alloc = {
                "bull": self.bull_allocation,
                "neutral": self.neutral_allocation,
                "bear": self.bear_allocation,
            }.get(regime, self.neutral_allocation)

            if alloc > 0:
                direction = SignalDirection.LONG
            elif alloc == 0 and not self.is_flat(symbol):
                direction = SignalDirection.FLAT
            else:
                continue

            signals.append(Signal(
                signal_id=uuid4(),
                strategy_id=self.strategy_id,
                symbol=symbol,
                direction=direction,
                strength=alloc,
                timestamp=now,
                metadata={"regime": regime, "allocation": alloc},
            ))

        return signals

    def calculate_target_positions(self) -> List[TargetPosition]:
        """Convert regime signals to target weights."""
        signals = self.generate_signals()
        self._signals.extend(signals)

        targets: List[TargetPosition] = []
        now = datetime.utcnow()

        for sig in signals:
            weight = sig.strength if sig.direction == SignalDirection.LONG else 0.0
            targets.append(TargetPosition(
                symbol=sig.symbol,
                strategy_id=self.strategy_id,
                target_quantity=0,
                target_weight=weight / max(len(self.symbols), 1),
                urgency=0.2,  # Low urgency for long-term timing
                timestamp=now,
            ))

        return targets
