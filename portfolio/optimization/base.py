"""Portfolio optimizer abstract base class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from core.types import PortfolioWeights


class PortfolioOptimizer(ABC):
    """Abstract base class for portfolio optimization algorithms.

    All optimizers take expected returns and a covariance matrix,
    and produce optimal portfolio weights.
    """

    @abstractmethod
    def optimize(
        self,
        expected_returns: pd.Series,
        cov_matrix: pd.DataFrame,
        constraints: Optional[Dict] = None,
    ) -> PortfolioWeights:
        """Compute optimal portfolio weights.

        Args:
            expected_returns: Expected returns per asset
            cov_matrix: Covariance matrix of returns
            constraints: Optional constraints (max_weight, min_weight, etc.)

        Returns:
            Dict of symbol -> weight
        """
        ...

    @staticmethod
    def normalize_weights(weights: PortfolioWeights, long_only: bool = True) -> PortfolioWeights:
        """Normalize weights to sum to 1 (or handle long/short)."""
        total = sum(abs(w) for w in weights.values())
        if total == 0:
            return weights
        if long_only:
            return {k: max(v, 0) / total for k, v in weights.items()}
        return {k: v / total for k, v in weights.items()}
