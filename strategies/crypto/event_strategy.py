"""Event-driven crypto strategy.

Trades on funding rate arbitrage, liquidation cascades, large transfers,
and macro events (CPI, FOMC). Combines on-chain and off-chain signals.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List
from uuid import uuid4

from core.enums import SignalDirection
from core.models import Bar, Signal
from strategies.base import StrategyBase, StrategyConfig
from strategies.registry import register_strategy


@register_strategy("crypto_event")
class EventDrivenStrategy(StrategyBase):
    """Event-driven trading: funding rates, liquidations, macro events."""

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

        # Parameters
        self.funding_threshold = self.get_param("funding_rate_threshold", 0.01)  # 1%
        self.liquidation_surge_mult = self.get_param("liquidation_surge_multiplier", 3.0)
        self.sentiment_threshold = self.get_param("sentiment_threshold", 0.7)
        self.holding_bars = self.get_param("holding_bars", 60)  # 1h on 1m bars
        self.max_positions = self.get_param("max_positions", 3)

        # State
        self._funding_rates: Dict[str, float] = {}
        self._liquidation_volume: Dict[str, float] = {}
        self._avg_liquidation: Dict[str, float] = {}
        self._sentiment_score: Dict[str, float] = {}
        self._bars_in_position: Dict[str, int] = {}
        self._active_events: List[Dict[str, Any]] = []

    def on_bar(self, symbol: str, bar: Bar) -> None:
        """Track bars for time-based exit."""
        super().on_bar(symbol, bar)
        if not self.is_flat(symbol):
            self._bars_in_position[symbol] = self._bars_in_position.get(symbol, 0) + 1
        else:
            self._bars_in_position[symbol] = 0

    def update_funding_rate(self, symbol: str, rate: float) -> None:
        """Update funding rate data (called from external data source)."""
        self._funding_rates[symbol] = rate

    def update_liquidation_data(self, symbol: str, volume: float) -> None:
        """Update liquidation cascade data."""
        prev_avg = self._avg_liquidation.get(symbol, volume)
        self._avg_liquidation[symbol] = 0.95 * prev_avg + 0.05 * volume
        self._liquidation_volume[symbol] = volume

    def update_sentiment(self, symbol: str, score: float) -> None:
        """Update sentiment score [-1, 1]."""
        self._sentiment_score[symbol] = score

    def generate_signals(self) -> List[Signal]:
        """Generate signals from event conditions."""
        signals: List[Signal] = []
        now = datetime.utcnow()
        active_positions = sum(1 for s in self.symbols if not self.is_flat(s))

        for symbol in self.symbols:
            # Time-based exit
            if not self.is_flat(symbol):
                if self._bars_in_position.get(symbol, 0) >= self.holding_bars:
                    signals.append(Signal(
                        signal_id=uuid4(),
                        strategy_id=self.strategy_id,
                        symbol=symbol,
                        direction=SignalDirection.FLAT,
                        strength=0.9,
                        timestamp=now,
                        metadata={"reason": "time_exit"},
                    ))
                continue

            if active_positions >= self.max_positions:
                continue

            # Event 1: Extreme funding rate (mean reversion)
            funding = self._funding_rates.get(symbol)
            if funding is not None and abs(funding) > self.funding_threshold:
                # Very high positive funding -> too many longs -> short
                direction = SignalDirection.SHORT if funding > 0 else SignalDirection.LONG
                strength = min(abs(funding) / self.funding_threshold * 0.5, 1.0)
                signals.append(Signal(
                    signal_id=uuid4(),
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=direction,
                    strength=strength,
                    timestamp=now,
                    metadata={"reason": "funding_rate", "funding": funding},
                ))
                continue

            # Event 2: Liquidation cascade (momentum follow)
            liq_vol = self._liquidation_volume.get(symbol, 0)
            avg_liq = self._avg_liquidation.get(symbol, 0)
            if avg_liq > 0 and liq_vol > self.liquidation_surge_mult * avg_liq:
                # Large liquidations usually push price further
                last_bar = self._last_bars.get(symbol)
                if last_bar:
                    price_change = (last_bar.close - last_bar.open) / last_bar.open if last_bar.open else 0
                    direction = SignalDirection.SHORT if price_change < 0 else SignalDirection.LONG
                    strength = min(liq_vol / (avg_liq * self.liquidation_surge_mult), 1.0)
                    signals.append(Signal(
                        signal_id=uuid4(),
                        strategy_id=self.strategy_id,
                        symbol=symbol,
                        direction=direction,
                        strength=strength * 0.6,
                        timestamp=now,
                        metadata={
                            "reason": "liquidation_cascade",
                            "liq_volume": liq_vol,
                            "avg_liq": avg_liq,
                        },
                    ))
                continue

            # Event 3: Extreme sentiment divergence
            sentiment = self._sentiment_score.get(symbol, 0)
            if abs(sentiment) > self.sentiment_threshold:
                direction = SignalDirection.LONG if sentiment > 0 else SignalDirection.SHORT
                signals.append(Signal(
                    signal_id=uuid4(),
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=direction,
                    strength=abs(sentiment) * 0.5,
                    timestamp=now,
                    metadata={"reason": "sentiment", "score": sentiment},
                ))

        return signals
