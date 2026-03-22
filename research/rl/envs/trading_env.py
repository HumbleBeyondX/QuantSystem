"""Gymnasium environment for single-asset trading.

Provides a discrete-action trading environment where an agent can hold, buy,
or sell a single asset based on observed price features.
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
            "Gymnasium is required for TradingEnv. "
            "Install it with: pip install gymnasium"
        )


if _HAS_GYM:
    _BaseEnv = gym.Env
else:
    _BaseEnv = object  # type: ignore[misc,assignment]


class TradingEnv(_BaseEnv):  # type: ignore[misc]
    """Single-asset trading environment.

    The agent observes a rolling window of price-derived features and chooses
    one of three discrete actions each step:

    - ``0``: Hold (do nothing)
    - ``1``: Buy (go long / increase position)
    - ``2``: Sell (go short / decrease position)

    Parameters:
        prices: 1-D array of asset prices.
        features: 2-D array of shape ``(n_timesteps, n_features)`` aligned
            with *prices*.  If ``None``, simple returns and log-returns are
            computed automatically.
        window_size: Number of past observations included in each state.
        initial_balance: Starting cash balance.
        transaction_cost: Proportional cost per trade (e.g. ``0.001``).
        max_position: Maximum absolute position size in units.

    Raises:
        ImportError: If Gymnasium is not installed.
    """

    metadata = {"render_modes": ["human"]}

    def __init__(
        self,
        prices: np.ndarray,
        features: Optional[np.ndarray] = None,
        window_size: int = 20,
        initial_balance: float = 100_000.0,
        transaction_cost: float = 0.001,
        max_position: int = 100,
    ) -> None:
        _check_gym()
        super().__init__()

        self._prices = np.asarray(prices, dtype=np.float64)
        self._window_size = window_size
        self._initial_balance = initial_balance
        self._transaction_cost = transaction_cost
        self._max_position = max_position

        # Build features if not provided
        if features is not None:
            self._features = np.asarray(features, dtype=np.float64)
        else:
            returns = np.diff(self._prices) / self._prices[:-1]
            log_returns = np.log(self._prices[1:] / self._prices[:-1])
            self._features = np.column_stack([returns, log_returns])
            # Align: features start at index 1 of prices
            self._prices = self._prices[1:]

        self._n_steps = len(self._prices)
        n_features = self._features.shape[1]

        # Spaces
        # Observation: window of features + [position, balance, unrealized_pnl]
        obs_dim = window_size * n_features + 3
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(obs_dim,), dtype=np.float64
        )
        self.action_space = spaces.Discrete(3)  # hold, buy, sell

        # State variables (set in reset)
        self._current_step: int = 0
        self._position: int = 0
        self._balance: float = initial_balance
        self._entry_price: float = 0.0

    # ------------------------------------------------------------------
    # Gymnasium API
    # ------------------------------------------------------------------

    def reset(
        self,
        *,
        seed: Optional[int] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> Tuple[np.ndarray, Dict[str, Any]]:
        """Reset the environment to its initial state.

        Returns:
            Tuple of (observation, info).
        """
        super().reset(seed=seed)
        self._current_step = self._window_size
        self._position = 0
        self._balance = self._initial_balance
        self._entry_price = 0.0
        return self._get_obs(), {}

    def step(
        self, action: int
    ) -> Tuple[np.ndarray, float, bool, bool, Dict[str, Any]]:
        """Execute one time step.

        Args:
            action: 0 = hold, 1 = buy, 2 = sell.

        Returns:
            Tuple of (observation, reward, terminated, truncated, info).
        """
        price = self._prices[self._current_step]
        prev_portfolio_value = self._portfolio_value(price)

        # Execute action
        if action == 1 and self._position < self._max_position:
            # Buy one unit
            cost = price * (1 + self._transaction_cost)
            if self._balance >= cost:
                self._balance -= cost
                self._position += 1
                if self._position == 1:
                    self._entry_price = price

        elif action == 2 and self._position > -self._max_position:
            # Sell one unit
            proceeds = price * (1 - self._transaction_cost)
            self._balance += proceeds
            self._position -= 1

        self._current_step += 1
        terminated = self._current_step >= self._n_steps - 1
        truncated = False

        new_price = self._prices[min(self._current_step, self._n_steps - 1)]
        new_portfolio_value = self._portfolio_value(new_price)

        reward = self._calculate_reward(prev_portfolio_value, new_portfolio_value)

        info = {
            "position": self._position,
            "balance": self._balance,
            "portfolio_value": new_portfolio_value,
            "price": new_price,
        }

        return self._get_obs(), reward, terminated, truncated, info

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_obs(self) -> np.ndarray:
        """Construct the observation vector."""
        start = max(0, self._current_step - self._window_size)
        end = self._current_step
        window = self._features[start:end]

        # Pad if at the beginning
        if window.shape[0] < self._window_size:
            pad = np.zeros((self._window_size - window.shape[0], window.shape[1]))
            window = np.vstack([pad, window])

        price = self._prices[min(self._current_step, self._n_steps - 1)]
        unrealized = self._position * (price - self._entry_price) if self._position != 0 else 0.0

        extra = np.array([
            self._position / self._max_position,
            self._balance / self._initial_balance,
            unrealized / self._initial_balance,
        ])

        return np.concatenate([window.flatten(), extra])

    def _calculate_reward(
        self, prev_value: float, curr_value: float
    ) -> float:
        """Compute step reward as log portfolio return."""
        if prev_value <= 0:
            return 0.0
        return float(np.log(max(curr_value, 1e-8) / prev_value))

    def _portfolio_value(self, price: float) -> float:
        return self._balance + self._position * price
