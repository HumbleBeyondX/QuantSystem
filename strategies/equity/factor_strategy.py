"""Multi-factor stock selection strategy.

Ranks stocks using a composite factor score (momentum, value, quality, volatility)
and constructs long/short portfolios. Rebalances on a configurable schedule.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

import numpy as np

from core.enums import SignalDirection
from core.models import Bar, Signal, TargetPosition
from strategies.base import StrategyBase, StrategyConfig
from strategies.registry import register_strategy


@register_strategy("equity_factor")
class FactorStrategy(StrategyBase):
    """Multi-factor stock selection with composite ranking."""

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

        # Parameters
        self.top_n = self.get_param("top_n", 10)
        self.bottom_n = self.get_param("bottom_n", 5)
        self.rebalance_frequency = self.get_param("rebalance_frequency", "weekly")
        self.long_only = self.get_param("long_only", True)

        # Factor weights
        self.factor_weights = self.get_param("factor_weights", {
            "momentum_63": 0.30,
            "realized_vol_21": -0.20,  # Negative = prefer low vol
            "rsi_factor": -0.10,       # Negative = prefer mean reversion
            "turnover_21": 0.10,
            "momentum_21": 0.30,
        })

        # State
        self._factor_scores: Dict[str, Dict[str, float]] = {}  # symbol -> {factor: score}
        self._composite_scores: Dict[str, float] = {}
        self._bars_since_rebalance = 0
        self._rebalance_interval = self._parse_rebalance_interval()

    def _parse_rebalance_interval(self) -> int:
        """Convert rebalance frequency to bar count (assuming daily bars)."""
        mapping = {"daily": 1, "weekly": 5, "biweekly": 10, "monthly": 21}
        return mapping.get(self.rebalance_frequency, 5)

    def on_bar(self, symbol: str, bar: Bar) -> None:
        """Track bars for rebalance timing."""
        super().on_bar(symbol, bar)
        self._bars_since_rebalance += 1

    def update_factor_scores(self, scores: Dict[str, Dict[str, float]]) -> None:
        """Update factor scores from the feature store.

        Args:
            scores: {symbol: {factor_name: z_score}}
        """
        self._factor_scores = scores
        self._compute_composite()

    def _compute_composite(self) -> None:
        """Compute composite score for each symbol."""
        self._composite_scores.clear()
        for symbol, factors in self._factor_scores.items():
            score = 0.0
            for factor_name, weight in self.factor_weights.items():
                if factor_name in factors:
                    score += weight * factors[factor_name]
            self._composite_scores[symbol] = score

    def generate_signals(self) -> List[Signal]:
        """Generate signals based on factor rankings at rebalance time."""
        if self._bars_since_rebalance < self._rebalance_interval:
            return []
        if not self._composite_scores:
            return []

        self._bars_since_rebalance = 0
        now = datetime.utcnow()
        signals: List[Signal] = []

        # Rank symbols by composite score
        ranked = sorted(
            self._composite_scores.items(),
            key=lambda x: x[1],
            reverse=True,
        )

        # Top N: long signals
        for symbol, score in ranked[:self.top_n]:
            signals.append(Signal(
                signal_id=uuid4(),
                strategy_id=self.strategy_id,
                symbol=symbol,
                direction=SignalDirection.LONG,
                strength=min(abs(score), 1.0),
                timestamp=now,
                metadata={"composite_score": score, "rank": ranked.index((symbol, score)) + 1},
            ))

        # Bottom N: short signals (if not long-only)
        if not self.long_only:
            for symbol, score in ranked[-self.bottom_n:]:
                signals.append(Signal(
                    signal_id=uuid4(),
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=SignalDirection.SHORT,
                    strength=min(abs(score), 1.0),
                    timestamp=now,
                    metadata={"composite_score": score},
                ))

        # Flat signals for symbols no longer in top/bottom
        active_symbols = set()
        for sig in signals:
            active_symbols.add(sig.symbol)
        for symbol in self.symbols:
            if symbol not in active_symbols and not self.is_flat(symbol):
                signals.append(Signal(
                    signal_id=uuid4(),
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=SignalDirection.FLAT,
                    strength=0.8,
                    timestamp=now,
                    metadata={"reason": "no_longer_in_selection"},
                ))

        return signals

    def calculate_target_positions(self) -> List[TargetPosition]:
        """Equal-weight allocation to selected stocks."""
        signals = self.generate_signals()
        self._signals.extend(signals)

        if not signals:
            return []

        longs = [s for s in signals if s.direction == SignalDirection.LONG]
        shorts = [s for s in signals if s.direction == SignalDirection.SHORT]
        flats = [s for s in signals if s.direction == SignalDirection.FLAT]

        targets: List[TargetPosition] = []
        now = datetime.utcnow()

        # Equal weight among longs
        if longs:
            weight = 1.0 / len(longs)
            for sig in longs:
                targets.append(TargetPosition(
                    symbol=sig.symbol,
                    strategy_id=self.strategy_id,
                    target_quantity=0,  # Will be computed by portfolio optimizer
                    target_weight=weight,
                    urgency=0.3,
                    timestamp=now,
                ))

        # Equal weight among shorts
        if shorts:
            weight = -1.0 / len(shorts)
            for sig in shorts:
                targets.append(TargetPosition(
                    symbol=sig.symbol,
                    strategy_id=self.strategy_id,
                    target_quantity=0,
                    target_weight=weight,
                    urgency=0.3,
                    timestamp=now,
                ))

        # Flat positions
        for sig in flats:
            targets.append(TargetPosition(
                symbol=sig.symbol,
                strategy_id=self.strategy_id,
                target_quantity=0,
                target_weight=0.0,
                urgency=0.5,
                timestamp=now,
            ))

        return targets
