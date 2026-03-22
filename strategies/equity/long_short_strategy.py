"""Long/short market-neutral equity strategy.

Pairs trading and statistical arbitrage using cointegration and
z-score mean reversion. Maintains dollar-neutral exposure.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Deque, Dict, List, Optional, Tuple
from uuid import uuid4

import numpy as np

from core.enums import SignalDirection
from core.models import Bar, Signal
from strategies.base import StrategyBase, StrategyConfig
from strategies.registry import register_strategy


@register_strategy("equity_long_short")
class LongShortStrategy(StrategyBase):
    """Market-neutral pairs trading via spread z-score."""

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

        # Parameters
        self.pairs: List[Tuple[str, str]] = [
            tuple(p) for p in self.get_param("pairs", [])
        ]
        self.zscore_entry = self.get_param("zscore_entry", 2.0)
        self.zscore_exit = self.get_param("zscore_exit", 0.5)
        self.zscore_stop = self.get_param("zscore_stop", 3.5)
        self.lookback = self.get_param("lookback", 60)
        self.hedge_ratio_window = self.get_param("hedge_ratio_window", 60)

        # State
        self._prices: Dict[str, Deque[float]] = {}
        self._hedge_ratios: Dict[Tuple[str, str], float] = {}

    def on_bar(self, symbol: str, bar: Bar) -> None:
        super().on_bar(symbol, bar)
        if symbol not in self._prices:
            self._prices[symbol] = deque(maxlen=max(self.lookback, self.hedge_ratio_window) + 10)
        self._prices[symbol].append(bar.close)

    def generate_signals(self) -> List[Signal]:
        """Generate pairs trading signals based on spread z-score."""
        signals: List[Signal] = []
        now = datetime.utcnow()

        for sym_a, sym_b in self.pairs:
            prices_a = self._prices.get(sym_a)
            prices_b = self._prices.get(sym_b)

            if not prices_a or not prices_b:
                continue
            if len(prices_a) < self.lookback or len(prices_b) < self.lookback:
                continue

            a = np.array(list(prices_a)[-self.lookback:])
            b = np.array(list(prices_b)[-self.lookback:])

            # Compute hedge ratio (OLS: a = beta * b + alpha)
            beta = np.cov(a, b)[0, 1] / np.var(b) if np.var(b) > 0 else 1.0
            self._hedge_ratios[(sym_a, sym_b)] = beta

            # Compute spread and z-score
            spread = a - beta * b
            spread_mean = np.mean(spread)
            spread_std = np.std(spread)
            if spread_std == 0:
                continue

            current_z = (spread[-1] - spread_mean) / spread_std

            pair_key = f"{sym_a}/{sym_b}"
            is_flat_a = self.is_flat(sym_a)
            is_flat_b = self.is_flat(sym_b)

            if is_flat_a and is_flat_b:
                # Entry: z-score exceeds threshold
                if current_z > self.zscore_entry:
                    # Spread too high: short A, long B
                    signals.extend([
                        Signal(
                            signal_id=uuid4(), strategy_id=self.strategy_id,
                            symbol=sym_a, direction=SignalDirection.SHORT,
                            strength=min(abs(current_z) / self.zscore_entry * 0.5, 1.0),
                            timestamp=now,
                            metadata={"pair": pair_key, "zscore": current_z, "side": "short_leg"},
                        ),
                        Signal(
                            signal_id=uuid4(), strategy_id=self.strategy_id,
                            symbol=sym_b, direction=SignalDirection.LONG,
                            strength=min(abs(current_z) / self.zscore_entry * 0.5, 1.0),
                            timestamp=now,
                            metadata={"pair": pair_key, "zscore": current_z, "side": "long_leg", "hedge_ratio": beta},
                        ),
                    ])

                elif current_z < -self.zscore_entry:
                    # Spread too low: long A, short B
                    signals.extend([
                        Signal(
                            signal_id=uuid4(), strategy_id=self.strategy_id,
                            symbol=sym_a, direction=SignalDirection.LONG,
                            strength=min(abs(current_z) / self.zscore_entry * 0.5, 1.0),
                            timestamp=now,
                            metadata={"pair": pair_key, "zscore": current_z, "side": "long_leg"},
                        ),
                        Signal(
                            signal_id=uuid4(), strategy_id=self.strategy_id,
                            symbol=sym_b, direction=SignalDirection.SHORT,
                            strength=min(abs(current_z) / self.zscore_entry * 0.5, 1.0),
                            timestamp=now,
                            metadata={"pair": pair_key, "zscore": current_z, "side": "short_leg", "hedge_ratio": beta},
                        ),
                    ])

            elif not is_flat_a or not is_flat_b:
                # Exit: z-score reverts or hits stop
                should_exit = (
                    abs(current_z) < self.zscore_exit
                    or abs(current_z) > self.zscore_stop
                )
                if should_exit:
                    if not is_flat_a:
                        signals.append(Signal(
                            signal_id=uuid4(), strategy_id=self.strategy_id,
                            symbol=sym_a, direction=SignalDirection.FLAT,
                            strength=0.9, timestamp=now,
                            metadata={"pair": pair_key, "zscore": current_z, "reason": "exit"},
                        ))
                    if not is_flat_b:
                        signals.append(Signal(
                            signal_id=uuid4(), strategy_id=self.strategy_id,
                            symbol=sym_b, direction=SignalDirection.FLAT,
                            strength=0.9, timestamp=now,
                            metadata={"pair": pair_key, "zscore": current_z, "reason": "exit"},
                        ))

        return signals
