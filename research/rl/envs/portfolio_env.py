"""Gymnasium environment for multi-asset portfolio allocation.

The agent observes features for N assets and outputs a continuous weight
vector representing the desired portfolio allocation.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

import numpy as np

try:
    import gymnasium as gym
    from gymnasium import spaces

    _HAS_GYM = True
except ImportError:
    _HAS_GYM = False

logger = logging.getLogger(__name__)


def _check_gym() -> None:
    if not _HAS_GYM:
        raise ImportError(
            "Gymnasium is required for PortfolioEnv. "
            "Install it with: pip install gymnasium"
        )


if _HAS_GYM:
    _BaseEnv = gym.Env
else:
    _BaseEnv = object  # type: ignore[misc,assignment]


class PortfolioEnv(_BaseEnv):  # type: ignore[misc]
    """Multi-asset portfolio environment with continuous weight allocation.

    The agent selects a weight vector summing to 1 across N assets (plus an
    implicit cash position).  The environment computes the portfolio return
    each step accounting for transaction costs from rebalancing.

    Parameters:
        returns: Array of asset returns with shape
            ``(n_timesteps, n_assets)``.
        features: Array of per-asset features with shape
            ``(n_timesteps, n_assets, n_features)``.  If ``None``, raw
            returns are used as the sole feature.
        initial_balance: Starting portfolio value.
        transaction_cost: Proportional rebalancing cost.
        window_size: Number of past observations in each state.

    Raises:
        ImportError: If Gymnasium is not installed.
    """

    metadata = {"render_modes": ["human"]}

    def __init__(
        self,
        returns: np.ndarray,
        features: Optional[np.ndarray] = None,
        initial_balance: float = 1_000_000.0,
        transaction_cost: float = 0.001,
        window_size: int = 20,
    ) -> None:
        _check_gym()
        super().__init__()

        self._returns = np.asarray(returns, dtype=np.float64)
        self._n_steps, self._n_assets = self._returns.shape
        self._initial_balance = initial_balance
        self._transaction_cost = transaction_cost
        self._window_size = window_size

        if features is not None:
            self._features = np.asarray(features, dtype=np.float64)
        else:
            # (n_steps, n_assets, 1) -- returns as the only feature
            self._features = self._returns[:, :, np.newaxis]

        self._n_features = self._features.shape[2]

        # Action: weight for each asset in [0, 1].  Normalised internally.
        self.action_space = spaces.Box(
            low=0.0, high=1.0, shape=(self._n_assets,), dtype=np.float64
        )

        # Observation: flattened feature window + current weights + balance
        obs_dim = (
            self._window_size * self._n_assets * self._n_features
            + self._n_assets  # current weights
            + 1  # portfolio value ratio
        )
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(obs_dim,), dtype=np.float64
        )

        # State
        self._current_step: int = 0
        self._weights: np.ndarray = np.zeros(self._n_assets)
        self._portfolio_value: float = initial_balance

    # ------------------------------------------------------------------
    # Gymnasium API
    # ------------------------------------------------------------------

    def reset(
        self,
        *,
        seed: Optional[int] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> Tuple[np.ndarray, Dict[str, Any]]:
        """Reset the environment."""
        super().reset(seed=seed)
        self._current_step = self._window_size
        self._weights = np.zeros(self._n_assets)
        self._portfolio_value = self._initial_balance
        return self._get_obs(), {}

    def step(
        self, action: np.ndarray
    ) -> Tuple[np.ndarray, float, bool, bool, Dict[str, Any]]:
        """Execute one rebalancing step.

        Args:
            action: Desired weight vector (non-negative).  Normalised
                internally so that weights sum to 1.

        Returns:
            Tuple of (observation, reward, terminated, truncated, info).
        """
        target_weights = self._normalise_weights(action)
        turnover = self._rebalance(target_weights)

        # Portfolio return for this step
        step_returns = self._returns[self._current_step]
        portfolio_return = float(np.dot(target_weights, step_returns))

        # Apply transaction cost proportional to turnover
        cost = turnover * self._transaction_cost
        net_return = portfolio_return - cost

        prev_value = self._portfolio_value
        self._portfolio_value *= 1.0 + net_return
        self._weights = target_weights * (1.0 + step_returns)
        weight_sum = self._weights.sum()
        if weight_sum > 0:
            self._weights /= weight_sum

        self._current_step += 1
        terminated = self._current_step >= self._n_steps - 1
        truncated = False

        reward = float(np.log(max(self._portfolio_value, 1e-8) / max(prev_value, 1e-8)))

        info = {
            "portfolio_value": self._portfolio_value,
            "weights": target_weights.copy(),
            "turnover": turnover,
            "net_return": net_return,
        }

        return self._get_obs(), reward, terminated, truncated, info

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_obs(self) -> np.ndarray:
        start = max(0, self._current_step - self._window_size)
        end = self._current_step
        window = self._features[start:end]

        if window.shape[0] < self._window_size:
            pad_shape = (
                self._window_size - window.shape[0],
                self._n_assets,
                self._n_features,
            )
            window = np.concatenate([np.zeros(pad_shape), window], axis=0)

        return np.concatenate([
            window.flatten(),
            self._weights,
            [self._portfolio_value / self._initial_balance],
        ])

    def _rebalance(self, target_weights: np.ndarray) -> float:
        """Compute turnover from current to target weights.

        Returns:
            Total absolute weight change (turnover).
        """
        turnover = float(np.sum(np.abs(target_weights - self._weights)))
        return turnover

    @staticmethod
    def _normalise_weights(weights: np.ndarray) -> np.ndarray:
        """Normalise weights to sum to 1, handling edge cases."""
        w = np.maximum(weights, 0.0)
        total = w.sum()
        if total > 0:
            return w / total
        # Equal weight fallback
        n = len(w)
        return np.ones(n) / n
