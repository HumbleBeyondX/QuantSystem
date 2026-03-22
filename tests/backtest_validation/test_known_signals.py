"""Backtest validation tests with known/deterministic signals.

These tests verify that the backtest engine produces correct PnL and metrics
when driven by a simple, predictable strategy.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Dict, List
from uuid import uuid4

import pandas as pd
import pytest

from backtesting.engine.event_driven import EventDrivenBacktest
from backtesting.metrics.performance import compute_metrics
from core.enums import SignalDirection
from core.models import Bar, Signal
from execution.models.fees import ZeroFee
from execution.models.slippage import ZeroSlippage
from strategies.base import StrategyBase, StrategyConfig


# ---------------------------------------------------------------------------
# Deterministic test strategy: always buy on bar 1, sell on bar N-1
# ---------------------------------------------------------------------------


class AlwaysBuyStrategy(StrategyBase):
    """Buys one unit on the first bar, sells on the last bar.

    For testing purposes only -- uses a counter to know when to trade.
    """

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)
        self._bar_count: Dict[str, int] = {}
        self._total_bars: int = config.parameters.get("total_bars", 10)

    def on_bar(self, symbol: str, bar: Bar) -> None:
        super().on_bar(symbol, bar)
        self._bar_count[symbol] = self._bar_count.get(symbol, 0) + 1

    def generate_signals(self) -> List[Signal]:
        signals: List[Signal] = []
        now = datetime.utcnow()

        for symbol in self.symbols:
            count = self._bar_count.get(symbol, 0)

            if count == 1 and self.is_flat(symbol):
                # Buy on first bar
                signals.append(Signal(
                    signal_id=uuid4(),
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=SignalDirection.LONG,
                    strength=1.0,
                    timestamp=now,
                    target_quantity=1.0,
                ))
            elif count >= self._total_bars - 1 and not self.is_flat(symbol):
                # Sell near the end
                signals.append(Signal(
                    signal_id=uuid4(),
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=SignalDirection.FLAT,
                    strength=1.0,
                    timestamp=now,
                    target_quantity=0.0,
                ))

        return signals


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_linear_data(
    symbol: str = "TEST",
    start_price: float = 100.0,
    end_price: float = 110.0,
    n_bars: int = 10,
) -> pd.DataFrame:
    """Create a DataFrame with a linear price ramp from *start_price* to *end_price*."""
    prices = [start_price + (end_price - start_price) * i / (n_bars - 1) for i in range(n_bars)]
    base_ts = datetime(2024, 1, 1, 9, 30, 0)
    rows = []
    for i, p in enumerate(prices):
        rows.append({
            "timestamp": base_ts + timedelta(minutes=i),
            "open": p - 0.1,
            "high": p + 0.5,
            "low": p - 0.5,
            "close": p,
            "volume": 1_000_000,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBacktestWithKnownSignals:
    @pytest.mark.asyncio
    async def test_backtest_with_known_signals(self):
        """Buy at bar 0, sell at bar N-1 on a rising price: PnL should be positive."""
        n_bars = 10
        data = {"TEST": _make_linear_data("TEST", 100.0, 110.0, n_bars)}

        config = StrategyConfig(
            strategy_id="always_buy",
            symbols=["TEST"],
            parameters={"total_bars": n_bars},
            mode="backtest",
        )
        strategy = AlwaysBuyStrategy(config)

        engine = EventDrivenBacktest(
            strategy=strategy,
            initial_cash=100_000.0,
            slippage=ZeroSlippage(),
            fees=ZeroFee(),
            risk_check=False,
        )

        results = await engine.run(data)

        # Strategy should have made at least one buy and one sell
        assert results["n_trades"] >= 2

        # Final value should exceed initial because price went up
        assert results["final_value"] >= results["initial_cash"]


class TestBacktestMetrics:
    @pytest.mark.asyncio
    async def test_backtest_metrics_calculation(self):
        """compute_metrics should return sensible values for a simple equity curve."""
        # Simulate a steadily rising equity curve
        n = 50
        values = [100_000 + i * 200 for i in range(n)]
        equity_df = pd.DataFrame({"total_value": values})

        metrics = compute_metrics(equity_df)

        assert metrics["total_return"] > 0
        assert metrics["total_return_pct"] > 0
        assert metrics["sharpe_ratio"] > 0
        assert metrics["max_drawdown"] >= 0
        assert metrics["max_drawdown"] < 1  # should not be 100%
        assert metrics["n_periods"] == n - 1  # pct_change drops first row
