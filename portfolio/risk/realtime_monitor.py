"""Real-time risk monitoring: VaR, CVaR, drawdown, Greeks exposure."""

from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, List

import numpy as np

from core.logging import get_logger
from core.models import PortfolioSnapshot

logger = get_logger(__name__)


class RealtimeRiskMonitor:
    """Monitors portfolio risk metrics in real-time."""

    def __init__(self, config: Dict[str, Any] = None) -> None:
        config = config or {}
        self.var_confidence = config.get("var_confidence", 0.95)
        self.max_drawdown_pct = config.get("max_drawdown_pct", 0.15)
        self.correlation_threshold = config.get("correlation_threshold", 0.85)
        self.lookback_returns = config.get("lookback_returns", 252)

        # State
        self._portfolio_values: Deque[float] = deque(maxlen=500)
        self._peak_value: float = 0.0
        self._returns: Deque[float] = deque(maxlen=self.lookback_returns)

    def update(self, portfolio: PortfolioSnapshot) -> List[Dict[str, Any]]:
        """Update risk metrics and return any alerts."""
        alerts: List[Dict[str, Any]] = []

        value = portfolio.total_value
        if self._portfolio_values:
            prev = self._portfolio_values[-1]
            if prev > 0:
                ret = (value - prev) / prev
                self._returns.append(ret)

        self._portfolio_values.append(value)
        self._peak_value = max(self._peak_value, value)

        # Drawdown check
        if self._peak_value > 0:
            drawdown = (self._peak_value - value) / self._peak_value
            if drawdown > self.max_drawdown_pct:
                alerts.append({
                    "type": "drawdown_warning",
                    "severity": "WARNING",
                    "drawdown_pct": drawdown,
                    "threshold": self.max_drawdown_pct,
                    "peak": self._peak_value,
                    "current": value,
                })

        return alerts

    def get_metrics(self, portfolio: PortfolioSnapshot) -> Dict[str, Any]:
        """Compute comprehensive risk metrics."""
        returns = np.array(self._returns) if self._returns else np.array([0.0])
        value = portfolio.total_value

        # Drawdown
        drawdown = 0.0
        if self._peak_value > 0:
            drawdown = (self._peak_value - value) / self._peak_value

        # VaR (Historical)
        var_95 = 0.0
        cvar_95 = 0.0
        if len(returns) > 10:
            sorted_returns = np.sort(returns)
            var_idx = int((1 - self.var_confidence) * len(sorted_returns))
            var_95 = -sorted_returns[var_idx] * value
            cvar_95 = -np.mean(sorted_returns[:var_idx + 1]) * value if var_idx > 0 else var_95

        # Volatility
        daily_vol = np.std(returns) if len(returns) > 1 else 0.0
        annual_vol = daily_vol * np.sqrt(252)

        # Sharpe estimate
        avg_return = np.mean(returns) if returns.size > 0 else 0.0
        sharpe = (avg_return * 252) / annual_vol if annual_vol > 0 else 0.0

        return {
            "total_value": value,
            "drawdown_pct": drawdown,
            "peak_value": self._peak_value,
            "var_95": var_95,
            "cvar_95": cvar_95,
            "daily_vol": daily_vol,
            "annual_vol": annual_vol,
            "sharpe_estimate": sharpe,
            "leverage": portfolio.leverage,
            "n_positions": len(portfolio.positions),
            "unrealized_pnl": portfolio.unrealized_pnl,
            "daily_pnl": portfolio.daily_pnl,
        }
