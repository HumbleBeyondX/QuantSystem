"""Vectorized backtest engine for fast parameter sweeps.

Uses pandas vectorized operations instead of event-by-event processing.
~100x faster than event-driven but less realistic (no slippage simulation).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from backtesting.metrics.performance import compute_metrics
from core.logging import get_logger

logger = get_logger(__name__)


class VectorizedBacktest:
    """Fast vectorized backtesting for signal-based strategies."""

    def __init__(
        self,
        initial_cash: float = 100_000,
        commission_pct: float = 0.001,
    ) -> None:
        self.initial_cash = initial_cash
        self.commission_pct = commission_pct

    def run(
        self,
        prices: pd.DataFrame,
        signals: pd.DataFrame,
        position_sizing: str = "equal_weight",
    ) -> Dict[str, Any]:
        """Run vectorized backtest.

        Args:
            prices: DataFrame with columns as symbols, index as dates, values as close prices
            signals: DataFrame same shape as prices, values in {-1, 0, 1}
            position_sizing: "equal_weight" | "signal_weighted"

        Returns:
            Dict with equity_curve, metrics, trades
        """
        # Compute returns
        returns = prices.pct_change().fillna(0)

        # Position weights
        if position_sizing == "equal_weight":
            n_positions = signals.abs().sum(axis=1).replace(0, 1)
            weights = signals.div(n_positions, axis=0)
        else:
            weights = signals

        # Strategy returns (weighted sum of asset returns)
        strategy_returns = (weights.shift(1) * returns).sum(axis=1)

        # Apply transaction costs
        turnover = weights.diff().abs().sum(axis=1).fillna(0)
        costs = turnover * self.commission_pct
        net_returns = strategy_returns - costs

        # Build equity curve
        equity = self.initial_cash * (1 + net_returns).cumprod()

        equity_df = pd.DataFrame({
            "total_value": equity,
            "returns": net_returns,
            "turnover": turnover,
        })
        equity_df.index.name = "timestamp"

        metrics = compute_metrics(equity_df)
        metrics["total_turnover"] = float(turnover.sum())
        metrics["avg_daily_turnover"] = float(turnover.mean())

        return {
            "equity_curve": equity_df.reset_index().to_dict("records"),
            "metrics": metrics,
            "final_value": float(equity.iloc[-1]) if len(equity) > 0 else self.initial_cash,
        }
