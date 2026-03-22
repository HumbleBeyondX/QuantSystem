"""Ensemble manager: runs multiple strategies and combines their signals."""

from __future__ import annotations

from typing import Dict, List

from core.logging import get_logger
from core.models import Signal, TargetPosition
from strategies.base import StrategyBase, StrategyState
from strategies.ensemble.signal_aggregator import SignalAggregator

logger = get_logger(__name__)


class EnsembleManager:
    """Manages multiple strategies and aggregates their outputs."""

    def __init__(
        self,
        strategies: List[StrategyBase],
        aggregator: SignalAggregator | None = None,
    ) -> None:
        self._strategies: Dict[str, StrategyBase] = {s.strategy_id: s for s in strategies}
        self._aggregator = aggregator or SignalAggregator()

    def initialize_all(self) -> None:
        for sid, strategy in self._strategies.items():
            try:
                strategy.initialize()
                logger.info("strategy_initialized", strategy_id=sid)
            except Exception as e:
                strategy.state = StrategyState.ERROR
                logger.error("strategy_init_failed", strategy_id=sid, error=str(e))

    def collect_signals(self) -> Dict[str, List[Signal]]:
        """Collect signals from all running strategies."""
        all_signals: Dict[str, List[Signal]] = {}
        for sid, strategy in self._strategies.items():
            if strategy.state != StrategyState.RUNNING:
                continue
            try:
                signals = strategy.generate_signals()
                if signals:
                    all_signals[sid] = signals
            except Exception as e:
                logger.error("signal_generation_error", strategy_id=sid, error=str(e))
        return all_signals

    def collect_target_positions(self) -> List[TargetPosition]:
        """Collect and aggregate target positions from all strategies."""
        all_signals = self.collect_signals()
        flat_signals = [s for signals in all_signals.values() for s in signals]

        if not flat_signals:
            return []

        return self._aggregator.aggregate(flat_signals)

    def get_strategy(self, strategy_id: str) -> StrategyBase | None:
        return self._strategies.get(strategy_id)

    def pause_strategy(self, strategy_id: str) -> None:
        strat = self._strategies.get(strategy_id)
        if strat:
            strat.state = StrategyState.PAUSED

    def resume_strategy(self, strategy_id: str) -> None:
        strat = self._strategies.get(strategy_id)
        if strat:
            strat.state = StrategyState.RUNNING

    def shutdown_all(self) -> None:
        for strategy in self._strategies.values():
            strategy.shutdown()

    @property
    def active_strategies(self) -> List[str]:
        return [sid for sid, s in self._strategies.items() if s.state == StrategyState.RUNNING]
