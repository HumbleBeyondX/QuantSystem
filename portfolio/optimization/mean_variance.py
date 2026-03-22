"""Mean-Variance Optimization (Markowitz)."""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

from core.logging import get_logger
from core.types import PortfolioWeights
from portfolio.optimization.base import PortfolioOptimizer

logger = get_logger(__name__)


class MeanVarianceOptimizer(PortfolioOptimizer):
    """Classical Markowitz Mean-Variance Optimization.

    Uses PyPortfolioOpt when available, falls back to analytical solution.
    """

    def __init__(
        self,
        target: str = "max_sharpe",  # "max_sharpe" | "min_vol" | "efficient_risk"
        risk_free_rate: float = 0.05,
        target_return: Optional[float] = None,
    ) -> None:
        self.target = target
        self.risk_free_rate = risk_free_rate
        self.target_return = target_return

    def optimize(
        self,
        expected_returns: pd.Series,
        cov_matrix: pd.DataFrame,
        constraints: Optional[Dict] = None,
    ) -> PortfolioWeights:
        constraints = constraints or {}
        max_weight = constraints.get("max_weight", 0.3)
        min_weight = constraints.get("min_weight", 0.0)

        try:
            from pypfopt import EfficientFrontier

            ef = EfficientFrontier(
                expected_returns,
                cov_matrix,
                weight_bounds=(min_weight, max_weight),
            )

            if self.target == "max_sharpe":
                ef.max_sharpe(risk_free_rate=self.risk_free_rate)
            elif self.target == "min_vol":
                ef.min_volatility()
            elif self.target == "efficient_risk" and self.target_return:
                ef.efficient_return(self.target_return)
            else:
                ef.max_sharpe(risk_free_rate=self.risk_free_rate)

            weights = ef.clean_weights()
            logger.info("mvo_optimized", target=self.target, weights=weights)
            return dict(weights)

        except ImportError:
            logger.warning("pypfopt not available, using analytical solution")
            return self._analytical_max_sharpe(expected_returns, cov_matrix, max_weight)

    def _analytical_max_sharpe(
        self, mu: pd.Series, cov: pd.DataFrame, max_w: float
    ) -> PortfolioWeights:
        """Simple analytical max Sharpe (equal weight as fallback)."""
        n = len(mu)
        try:
            cov_inv = np.linalg.inv(cov.values)
            excess = mu.values - self.risk_free_rate
            raw_weights = cov_inv @ excess
            raw_weights = np.maximum(raw_weights, 0)
            total = raw_weights.sum()
            if total > 0:
                weights = raw_weights / total
                weights = np.minimum(weights, max_w)
                weights /= weights.sum()
            else:
                weights = np.ones(n) / n
        except np.linalg.LinAlgError:
            weights = np.ones(n) / n

        return {sym: float(w) for sym, w in zip(mu.index, weights)}
