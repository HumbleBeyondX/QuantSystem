"""Unit tests for strategy registry, signal aggregation, and strategy initialisation."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest

from core.enums import AssetClass, SignalDirection
from core.models import Signal
from strategies.base import StrategyBase, StrategyConfig
from strategies.ensemble.signal_aggregator import SignalAggregator
from strategies.registry import (
    _REGISTRY,
    create_strategy,
    discover_strategies,
    get_strategy_class,
    list_strategies,
    register_strategy,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = datetime(2024, 6, 1, 12, 0, 0)


def _signal(
    strategy_id: str,
    symbol: str,
    direction: SignalDirection,
    strength: float,
) -> Signal:
    return Signal(
        signal_id=uuid4(),
        strategy_id=strategy_id,
        symbol=symbol,
        direction=direction,
        strength=strength,
        timestamp=_TS,
    )


# ---------------------------------------------------------------------------
# Registry Tests
# ---------------------------------------------------------------------------


class TestStrategyRegistry:
    def test_strategy_registry_discovery(self):
        """discover_strategies should populate the global registry."""
        discover_strategies()
        available = list_strategies()
        # At minimum, crypto_swing should be present (has @register_strategy)
        assert "crypto_swing" in available

    def test_registry_create_known_strategy(self):
        """create_strategy should instantiate a registered strategy."""
        discover_strategies()
        config = StrategyConfig(
            strategy_id="test_swing",
            name="crypto_swing",
            symbols=["BTC/USDT"],
        )
        strategy = create_strategy("crypto_swing", config)
        assert isinstance(strategy, StrategyBase)
        assert strategy.strategy_id == "test_swing"

    def test_registry_unknown_strategy_raises(self):
        """create_strategy should raise ValueError for unknown names."""
        with pytest.raises(ValueError, match="Unknown strategy"):
            create_strategy(
                "nonexistent_strategy",
                StrategyConfig(strategy_id="x"),
            )


# ---------------------------------------------------------------------------
# Signal Aggregator Tests
# ---------------------------------------------------------------------------


class TestSignalAggregator:
    def test_signal_aggregator_single(self):
        """A single signal should pass through unchanged."""
        agg = SignalAggregator(conflict_resolution="strongest")
        signals = [_signal("s1", "AAPL", SignalDirection.LONG, 0.8)]
        targets = agg.aggregate(signals)

        assert len(targets) == 1
        assert targets[0].symbol == "AAPL"
        assert targets[0].target_weight > 0  # positive = long

    def test_signal_aggregator_conflict_cancel(self):
        """Conflicting long/short signals should cancel when mode is 'cancel'."""
        agg = SignalAggregator(conflict_resolution="cancel")
        signals = [
            _signal("s1", "AAPL", SignalDirection.LONG, 0.7),
            _signal("s2", "AAPL", SignalDirection.SHORT, 0.6),
        ]
        targets = agg.aggregate(signals)

        assert len(targets) == 1
        # Should resolve to FLAT (target_quantity == 0)
        assert targets[0].target_quantity == 0

    def test_signal_aggregator_strongest(self):
        """With 'strongest' resolution, the strongest signal should win."""
        agg = SignalAggregator(conflict_resolution="strongest")
        signals = [
            _signal("s1", "AAPL", SignalDirection.LONG, 0.9),
            _signal("s2", "AAPL", SignalDirection.SHORT, 0.3),
        ]
        targets = agg.aggregate(signals)

        assert len(targets) == 1
        # Strongest is LONG with 0.9 => positive target_weight
        assert targets[0].target_weight > 0

    def test_signal_aggregator_multiple_symbols(self):
        """Aggregator should produce one target per symbol."""
        agg = SignalAggregator(conflict_resolution="strongest")
        signals = [
            _signal("s1", "AAPL", SignalDirection.LONG, 0.8),
            _signal("s1", "MSFT", SignalDirection.SHORT, 0.6),
        ]
        targets = agg.aggregate(signals)

        symbols = {t.symbol for t in targets}
        assert symbols == {"AAPL", "MSFT"}


# ---------------------------------------------------------------------------
# Swing Strategy Tests
# ---------------------------------------------------------------------------


class TestSwingStrategy:
    def test_swing_strategy_initialization(self):
        """SwingStrategy should initialise with default parameters."""
        discover_strategies()
        config = StrategyConfig(
            strategy_id="test_swing",
            name="crypto_swing",
            symbols=["BTC/USDT"],
            parameters={"ema_fast": 10, "ema_slow": 30},
        )
        strategy = create_strategy("crypto_swing", config)
        strategy.initialize()

        assert strategy.ema_fast == 10
        assert strategy.ema_slow == 30
        assert strategy.strategy_id == "test_swing"
