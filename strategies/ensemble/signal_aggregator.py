"""Signal aggregator: combine signals from multiple strategies.

Supports equal-weight, Sharpe-weighted, and confidence-weighted aggregation.
Resolves conflicts when multiple strategies signal on the same symbol.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Dict, List
from uuid import uuid4

from core.enums import SignalDirection
from core.models import Signal, TargetPosition


class SignalAggregator:
    """Aggregates signals from multiple strategies into unified target positions."""

    def __init__(
        self,
        method: str = "confidence_weighted",
        strategy_weights: Dict[str, float] | None = None,
        conflict_resolution: str = "strongest",  # "strongest" | "cancel" | "average"
    ) -> None:
        self.method = method
        self.strategy_weights = strategy_weights or {}
        self.conflict_resolution = conflict_resolution

    def aggregate(self, signals: List[Signal]) -> List[TargetPosition]:
        """Aggregate all signals into target positions."""
        # Group by symbol
        by_symbol: Dict[str, List[Signal]] = defaultdict(list)
        for sig in signals:
            by_symbol[sig.symbol].append(sig)

        targets: List[TargetPosition] = []
        now = datetime.utcnow()

        for symbol, sym_signals in by_symbol.items():
            direction, weight, urgency = self._resolve(sym_signals)

            if direction == SignalDirection.FLAT:
                targets.append(TargetPosition(
                    symbol=symbol,
                    strategy_id="ensemble",
                    target_quantity=0,
                    target_weight=0.0,
                    urgency=urgency,
                    timestamp=now,
                ))
            else:
                sign = 1.0 if direction == SignalDirection.LONG else -1.0
                targets.append(TargetPosition(
                    symbol=symbol,
                    strategy_id="ensemble",
                    target_quantity=0,  # To be filled by portfolio optimizer
                    target_weight=sign * weight,
                    urgency=urgency,
                    timestamp=now,
                ))

        return targets

    def _resolve(self, signals: List[Signal]) -> tuple:
        """Resolve signals for a single symbol.

        Returns: (direction, weight, urgency)
        """
        if len(signals) == 1:
            sig = signals[0]
            return sig.direction, sig.strength, 0.5

        # Separate by direction
        longs = [s for s in signals if s.direction == SignalDirection.LONG]
        shorts = [s for s in signals if s.direction == SignalDirection.SHORT]
        flats = [s for s in signals if s.direction == SignalDirection.FLAT]

        # If any strategy says flat, prioritize exit
        if flats:
            return SignalDirection.FLAT, max(s.strength for s in flats), 0.8

        if self.conflict_resolution == "cancel":
            # Conflicting directions cancel out
            if longs and shorts:
                return SignalDirection.FLAT, 0, 0.3
        elif self.conflict_resolution == "average":
            # Net direction
            net = sum(s.strength for s in longs) - sum(s.strength for s in shorts)
            if abs(net) < 0.1:
                return SignalDirection.FLAT, 0, 0.3
            direction = SignalDirection.LONG if net > 0 else SignalDirection.SHORT
            return direction, min(abs(net), 1.0), 0.5

        # Default: strongest signal wins
        all_signals = longs + shorts
        best = max(all_signals, key=lambda s: self._weighted_strength(s))
        return best.direction, best.strength, 0.5

    def _weighted_strength(self, signal: Signal) -> float:
        """Apply strategy weights to signal strength."""
        weight = self.strategy_weights.get(signal.strategy_id, 1.0)
        return signal.strength * weight
