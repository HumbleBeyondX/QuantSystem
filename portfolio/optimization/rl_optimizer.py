"""RL-based dynamic portfolio optimizer.

Uses a trained RL agent (from research/rl/) to dynamically allocate
portfolio weights based on market features. Placeholder for Phase 4.
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

from core.logging import get_logger
from core.types import PortfolioWeights
from portfolio.optimization.base import PortfolioOptimizer

logger = get_logger(__name__)


class RLPortfolioOptimizer(PortfolioOptimizer):
    """RL-based portfolio optimizer using a pre-trained agent.

    The agent observes market features and outputs portfolio weights.
    This is a placeholder that will be connected to the RL training
    pipeline in Phase 4.
    """

    def __init__(self, model_path: Optional[str] = None) -> None:
        self.model_path = model_path
        self._model = None

    def load_model(self, path: str) -> None:
        """Load a trained RL model."""
        try:
            from stable_baselines3 import PPO
            self._model = PPO.load(path)
            logger.info("rl_optimizer_model_loaded", path=path)
        except Exception as e:
            logger.error("rl_optimizer_load_failed", error=str(e))

    def optimize(
        self,
        expected_returns: pd.Series,
        cov_matrix: pd.DataFrame,
        constraints: Optional[Dict] = None,
    ) -> PortfolioWeights:
        assets = list(expected_returns.index)
        n = len(assets)

        if self._model is None:
            logger.warning("rl_optimizer_no_model, falling back to equal weight")
            return {sym: 1.0 / n for sym in assets}

        # Construct observation from returns and covariance
        obs = np.concatenate([
            expected_returns.values,
            np.diag(cov_matrix.values),  # Variances
        ]).astype(np.float32)

        action, _ = self._model.predict(obs, deterministic=True)

        # Action space: weights (softmax to ensure they sum to 1)
        weights = np.exp(action) / np.exp(action).sum()

        # Apply constraints
        if constraints:
            max_w = constraints.get("max_weight", 0.5)
            weights = np.minimum(weights, max_w)
            weights /= weights.sum()

        return {sym: float(w) for sym, w in zip(assets, weights)}
