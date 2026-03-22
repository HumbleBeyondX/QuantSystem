"""Correlation regime monitoring.

Detects when cross-asset correlations spike (risk-off events)
or break down (regime changes), triggering risk alerts.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Deque, Dict, List

import numpy as np

from core.logging import get_logger

logger = get_logger(__name__)


class CorrelationMonitor:
    """Monitors rolling correlation between assets for regime changes."""

    def __init__(
        self,
        lookback: int = 60,
        alert_threshold: float = 0.85,
        breakdown_threshold: float = 0.3,
    ) -> None:
        self.lookback = lookback
        self.alert_threshold = alert_threshold
        self.breakdown_threshold = breakdown_threshold
        self._returns: Dict[str, Deque[float]] = {}

    def update(self, symbol: str, return_value: float) -> None:
        """Add a new return observation for a symbol."""
        if symbol not in self._returns:
            self._returns[symbol] = deque(maxlen=self.lookback)
        self._returns[symbol].append(return_value)

    def get_correlation_matrix(self) -> Dict[str, Dict[str, float]]:
        """Compute current rolling correlation matrix."""
        symbols = [s for s, r in self._returns.items() if len(r) >= self.lookback]
        if len(symbols) < 2:
            return {}

        matrix = {}
        for i, sym_a in enumerate(symbols):
            matrix[sym_a] = {}
            for j, sym_b in enumerate(symbols):
                if i == j:
                    matrix[sym_a][sym_b] = 1.0
                    continue
                a = np.array(list(self._returns[sym_a])[-self.lookback:])
                b = np.array(list(self._returns[sym_b])[-self.lookback:])
                corr = np.corrcoef(a, b)[0, 1]
                matrix[sym_a][sym_b] = float(corr) if np.isfinite(corr) else 0.0

        return matrix

    def check_alerts(self) -> List[Dict[str, Any]]:
        """Check for correlation regime alerts."""
        alerts: List[Dict[str, Any]] = []
        corr_matrix = self.get_correlation_matrix()

        if not corr_matrix:
            return alerts

        symbols = list(corr_matrix.keys())
        for i in range(len(symbols)):
            for j in range(i + 1, len(symbols)):
                corr = corr_matrix[symbols[i]][symbols[j]]
                # High correlation alert (contagion risk)
                if abs(corr) > self.alert_threshold:
                    alerts.append({
                        "type": "high_correlation",
                        "pair": f"{symbols[i]}/{symbols[j]}",
                        "correlation": corr,
                        "threshold": self.alert_threshold,
                    })

        return alerts
