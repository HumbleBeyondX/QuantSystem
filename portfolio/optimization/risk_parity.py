"""Risk Parity / Hierarchical Risk Parity (HRP) optimization."""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

from core.logging import get_logger
from core.types import PortfolioWeights
from portfolio.optimization.base import PortfolioOptimizer

logger = get_logger(__name__)


class RiskParityOptimizer(PortfolioOptimizer):
    """Equal risk contribution portfolio.

    Each asset contributes equally to total portfolio risk.
    Uses inverse-volatility as a simple approximation,
    or PyPortfolioOpt's HRP for the full hierarchical approach.
    """

    def __init__(self, method: str = "inverse_vol") -> None:
        """
        Args:
            method: "inverse_vol" (simple) or "hrp" (hierarchical risk parity)
        """
        self.method = method

    def optimize(
        self,
        expected_returns: pd.Series,
        cov_matrix: pd.DataFrame,
        constraints: Optional[Dict] = None,
    ) -> PortfolioWeights:
        assets = list(cov_matrix.columns)

        if self.method == "hrp":
            return self._hrp(cov_matrix, assets)

        return self._inverse_vol(cov_matrix, assets)

    def _inverse_vol(self, cov: pd.DataFrame, assets: list) -> PortfolioWeights:
        """Simple inverse volatility weighting."""
        vols = np.sqrt(np.diag(cov.values))
        inv_vols = 1.0 / np.where(vols > 0, vols, 1.0)
        weights = inv_vols / inv_vols.sum()
        return {sym: float(w) for sym, w in zip(assets, weights)}

    def _hrp(self, cov: pd.DataFrame, assets: list) -> PortfolioWeights:
        """Hierarchical Risk Parity (Marcos Lopez de Prado)."""
        try:
            from pypfopt import HRPOpt

            hrp = HRPOpt(returns=None, cov_matrix=cov)
            weights = hrp.optimize()
            logger.info("hrp_optimized")
            return dict(weights)
        except ImportError:
            logger.warning("pypfopt not available, falling back to inverse vol")
            return self._inverse_vol(cov, assets)
