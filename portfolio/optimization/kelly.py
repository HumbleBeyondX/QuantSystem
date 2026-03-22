"""Kelly criterion position sizing."""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

from core.types import PortfolioWeights
from portfolio.optimization.base import PortfolioOptimizer


class KellyCriterion(PortfolioOptimizer):
    """Kelly criterion for optimal bet sizing.

    Full Kelly: f* = (p * b - q) / b where p=win_prob, b=win/loss ratio, q=1-p
    Fractional Kelly applies a fraction (e.g., 0.25) to reduce volatility.
    """

    def __init__(self, fraction: float = 0.25) -> None:
        self.fraction = fraction

    def optimize(
        self,
        expected_returns: pd.Series,
        cov_matrix: pd.DataFrame,
        constraints: Optional[Dict] = None,
    ) -> PortfolioWeights:
        """Multi-asset Kelly: f* = Sigma^{-1} * mu (scaled by fraction)."""
        try:
            cov_inv = np.linalg.inv(cov_matrix.values)
            raw = cov_inv @ expected_returns.values
            # Apply fractional Kelly
            raw *= self.fraction
            # Clip to reasonable bounds
            raw = np.clip(raw, -0.5, 0.5)
            return {sym: float(w) for sym, w in zip(expected_returns.index, raw)}
        except np.linalg.LinAlgError:
            n = len(expected_returns)
            return {sym: 1.0 / n for sym in expected_returns.index}

    @staticmethod
    def single_asset_kelly(
        win_prob: float,
        win_loss_ratio: float,
        fraction: float = 0.25,
    ) -> float:
        """Classic Kelly for a single binary bet."""
        q = 1 - win_prob
        f_star = (win_prob * win_loss_ratio - q) / win_loss_ratio
        return max(0, f_star * fraction)
