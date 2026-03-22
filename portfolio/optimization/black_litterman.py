"""Black-Litterman portfolio optimization.

Combines market equilibrium returns with investor views (from ML/strategies)
to produce posterior expected returns, then optimizes weights.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.logging import get_logger
from core.types import PortfolioWeights
from portfolio.optimization.base import PortfolioOptimizer

logger = get_logger(__name__)


class BlackLittermanOptimizer(PortfolioOptimizer):
    """Black-Litterman model with views from alpha signals."""

    def __init__(
        self,
        risk_aversion: float = 2.5,
        tau: float = 0.05,
        risk_free_rate: float = 0.05,
    ) -> None:
        self.risk_aversion = risk_aversion
        self.tau = tau
        self.risk_free_rate = risk_free_rate
        self._views: List[Tuple[Dict[str, float], float, float]] = []

    def add_view(
        self,
        asset_weights: Dict[str, float],
        expected_return: float,
        confidence: float = 0.5,
    ) -> None:
        """Add an investor view.

        Args:
            asset_weights: {symbol: weight} for this view.
                           Absolute: {"AAPL": 1} means "AAPL will return X%"
                           Relative: {"AAPL": 1, "MSFT": -1} means "AAPL will outperform MSFT by X%"
            expected_return: Expected return for this view (annualized)
            confidence: Confidence level [0, 1]
        """
        self._views.append((asset_weights, expected_return, confidence))

    def clear_views(self) -> None:
        self._views.clear()

    def optimize(
        self,
        expected_returns: pd.Series,
        cov_matrix: pd.DataFrame,
        constraints: Optional[Dict] = None,
    ) -> PortfolioWeights:
        constraints = constraints or {}
        assets = list(expected_returns.index)
        n = len(assets)

        try:
            from pypfopt import BlackLittermanModel, EfficientFrontier
            from pypfopt import risk_models

            # Build P (pick matrix) and Q (view returns) from views
            if self._views:
                P = np.zeros((len(self._views), n))
                Q = np.zeros(len(self._views))
                omega_diag = np.zeros(len(self._views))

                for i, (weights, ret, conf) in enumerate(self._views):
                    Q[i] = ret
                    for sym, w in weights.items():
                        if sym in assets:
                            P[i, assets.index(sym)] = w
                    # Omega: uncertainty of view (lower confidence = higher uncertainty)
                    omega_diag[i] = (1 - conf) * self.tau * 0.1

                omega = np.diag(omega_diag)

                bl = BlackLittermanModel(
                    cov_matrix,
                    pi="market",
                    absolute_views=dict(zip(
                        [a for a in assets if any(a in v[0] for v in self._views)],
                        [v[1] for v in self._views if len(v[0]) == 1],
                    )) if any(len(v[0]) == 1 for v in self._views) else None,
                    Q=Q if len(self._views) > 0 else None,
                    P=P if len(self._views) > 0 else None,
                    omega=omega if len(self._views) > 0 else None,
                    risk_aversion=self.risk_aversion,
                    tau=self.tau,
                )
                posterior_returns = bl.bl_returns()
                posterior_cov = bl.bl_cov()
            else:
                posterior_returns = expected_returns
                posterior_cov = cov_matrix

            # Optimize with posterior
            max_weight = constraints.get("max_weight", 0.3)
            ef = EfficientFrontier(
                posterior_returns, posterior_cov,
                weight_bounds=(0, max_weight),
            )
            ef.max_sharpe(risk_free_rate=self.risk_free_rate)
            weights = ef.clean_weights()
            logger.info("bl_optimized", n_views=len(self._views))
            return dict(weights)

        except ImportError:
            logger.warning("pypfopt not available, falling back to equal weight")
            w = 1.0 / n
            return {sym: w for sym in assets}
