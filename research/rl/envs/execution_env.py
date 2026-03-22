"""Gymnasium environment for optimal order execution.

Models the problem of executing a large order over a fixed time horizon while
minimising market impact and slippage, similar to TWAP/VWAP benchmark
strategies.
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
            "Gymnasium is required for ExecutionEnv. "
            "Install it with: pip install gymnasium"
        )


if _HAS_GYM:
    _BaseEnv = gym.Env
else:
    _BaseEnv = object  # type: ignore[misc,assignment]


class ExecutionEnv(_BaseEnv):  # type: ignore[misc]
    """Order execution optimisation environment.

    The agent must execute a given total quantity over a fixed number of time
    steps.  At each step the agent observes remaining quantity, time
    remaining, and market microstructure features, then decides what fraction
    of the remaining order to execute immediately.

    The reward penalises deviation from a TWAP benchmark and accounts for a
    simple temporary market-impact model:

        impact = impact_coeff * (fraction_executed / volume) ** impact_exponent

    Parameters:
        total_quantity: Total number of shares/units to execute.
        n_steps: Number of time slices in the execution horizon.
        prices: 1-D array of mid-prices for each time step.  If ``None``,
            prices are simulated via geometric Brownian motion.
        volumes: 1-D array of market volume per step.  If ``None``, volumes
            are sampled uniformly.
        impact_coeff: Market impact coefficient.
        impact_exponent: Market impact exponent (1 = linear, 2 = quadratic).
        features: Optional 2-D array ``(n_steps, n_features)`` of additional
            market features (e.g. spread, volatility).

    Raises:
        ImportError: If Gymnasium is not installed.
    """

    metadata = {"render_modes": ["human"]}

    def __init__(
        self,
        total_quantity: float = 10_000.0,
        n_steps: int = 50,
        prices: Optional[np.ndarray] = None,
        volumes: Optional[np.ndarray] = None,
        impact_coeff: float = 0.1,
        impact_exponent: float = 1.0,
        features: Optional[np.ndarray] = None,
    ) -> None:
        _check_gym()
        super().__init__()

        self._total_quantity = total_quantity
        self._n_steps = n_steps
        self._impact_coeff = impact_coeff
        self._impact_exponent = impact_exponent

        # Market data (may be regenerated on reset)
        self._prices = (
            np.asarray(prices, dtype=np.float64) if prices is not None else None
        )
        self._volumes = (
            np.asarray(volumes, dtype=np.float64) if volumes is not None else None
        )
        self._external_features = (
            np.asarray(features, dtype=np.float64) if features is not None else None
        )

        # Observation: [remaining_qty_frac, time_remaining_frac, price_return,
        #               volume_ratio, + optional external features]
        n_base_features = 4
        n_extra = self._external_features.shape[1] if self._external_features is not None else 0
        obs_dim = n_base_features + n_extra

        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(obs_dim,), dtype=np.float64
        )

        # Action: fraction of remaining quantity to execute now, in [0, 1]
        self.action_space = spaces.Box(
            low=0.0, high=1.0, shape=(1,), dtype=np.float64
        )

        # State variables
        self._current_step: int = 0
        self._remaining_qty: float = total_quantity
        self._exec_prices: list = []
        self._exec_quantities: list = []
        self._sim_prices: Optional[np.ndarray] = None
        self._sim_volumes: Optional[np.ndarray] = None

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
        self._current_step = 0
        self._remaining_qty = self._total_quantity
        self._exec_prices = []
        self._exec_quantities = []

        # Generate or reuse market data
        if self._prices is not None:
            self._sim_prices = self._prices.copy()
        else:
            self._sim_prices = self._simulate_prices()

        if self._volumes is not None:
            self._sim_volumes = self._volumes.copy()
        else:
            rng = self.np_random if hasattr(self, "np_random") else np.random.default_rng(seed)
            self._sim_volumes = rng.uniform(
                self._total_quantity * 0.5,
                self._total_quantity * 2.0,
                size=self._n_steps,
            )

        return self._get_obs(), {}

    def step(
        self, action: np.ndarray
    ) -> Tuple[np.ndarray, float, bool, bool, Dict[str, Any]]:
        """Execute one time slice.

        Args:
            action: 1-D array with a single element in [0, 1] representing
                the fraction of remaining quantity to execute now.

        Returns:
            Tuple of (observation, reward, terminated, truncated, info).
        """
        fraction = float(np.clip(action[0], 0.0, 1.0))

        # On the last step, force execution of everything remaining
        is_last_step = self._current_step >= self._n_steps - 1
        if is_last_step:
            fraction = 1.0

        qty_to_execute = fraction * self._remaining_qty
        if qty_to_execute < 1e-8:
            qty_to_execute = 0.0

        # Market impact
        mid_price = self._sim_prices[self._current_step]
        volume = self._sim_volumes[self._current_step]
        participation = qty_to_execute / max(volume, 1e-8)

        impact = self._impact_coeff * (participation ** self._impact_exponent)
        exec_price = mid_price * (1.0 + impact)

        # Record
        if qty_to_execute > 0:
            self._exec_prices.append(exec_price)
            self._exec_quantities.append(qty_to_execute)
        self._remaining_qty -= qty_to_execute

        # Reward: negative implementation shortfall vs TWAP
        twap_price = mid_price  # TWAP benchmark is the current mid
        shortfall = (exec_price - twap_price) * qty_to_execute
        reward = -shortfall / (self._total_quantity * mid_price + 1e-8)

        # Penalise remaining inventory at end
        if is_last_step and self._remaining_qty > 1e-8:
            reward -= 1.0  # harsh penalty for unexecuted quantity

        self._current_step += 1
        terminated = self._current_step >= self._n_steps
        truncated = False

        vwap = self._compute_vwap()
        info = {
            "remaining_qty": self._remaining_qty,
            "exec_price": exec_price if qty_to_execute > 0 else 0.0,
            "impact": impact,
            "vwap": vwap,
            "participation_rate": participation,
        }

        return self._get_obs(), reward, terminated, truncated, info

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_obs(self) -> np.ndarray:
        step = min(self._current_step, self._n_steps - 1)
        remaining_frac = self._remaining_qty / self._total_quantity
        time_remaining_frac = 1.0 - (self._current_step / self._n_steps)

        price = self._sim_prices[step]
        prev_price = self._sim_prices[max(0, step - 1)]
        price_return = (price - prev_price) / (prev_price + 1e-8)

        volume = self._sim_volumes[step]
        avg_volume = self._sim_volumes[:step + 1].mean() if step > 0 else volume
        volume_ratio = volume / (avg_volume + 1e-8)

        base = np.array([
            remaining_frac,
            time_remaining_frac,
            price_return,
            volume_ratio,
        ])

        if self._external_features is not None:
            extra = self._external_features[step]
            return np.concatenate([base, extra])
        return base

    def _compute_vwap(self) -> float:
        """Compute VWAP of executed trades so far."""
        if not self._exec_quantities:
            return 0.0
        prices = np.array(self._exec_prices)
        qtys = np.array(self._exec_quantities)
        return float(np.dot(prices, qtys) / qtys.sum())

    def _simulate_prices(self) -> np.ndarray:
        """Simulate prices via geometric Brownian motion."""
        rng = self.np_random if hasattr(self, "np_random") else np.random.default_rng()
        dt = 1.0 / self._n_steps
        mu = 0.0
        sigma = 0.02
        log_returns = rng.normal(mu * dt, sigma * np.sqrt(dt), self._n_steps)
        prices = 100.0 * np.exp(np.cumsum(log_returns))
        return prices
