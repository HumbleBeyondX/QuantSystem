"""Unit tests for portfolio optimization and position management."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from portfolio.optimization.mean_variance import MeanVarianceOptimizer
from portfolio.optimization.kelly import KellyCriterion
from portfolio.optimization.risk_parity import RiskParityOptimizer
from portfolio.optimization.base import PortfolioOptimizer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_returns(n_assets: int = 4, n_days: int = 252, seed: int = 42) -> pd.DataFrame:
    """Generate synthetic daily returns for multiple assets."""
    rng = np.random.RandomState(seed)
    symbols = [f"ASSET_{i}" for i in range(n_assets)]
    data = rng.randn(n_days, n_assets) * 0.01 + 0.0003  # slight positive drift
    return pd.DataFrame(data, columns=symbols)


def _expected_returns_and_cov(returns: pd.DataFrame):
    """Compute expected returns and covariance from returns DataFrame."""
    mu = returns.mean() * 252
    cov = returns.cov() * 252
    return mu, cov


# ---------------------------------------------------------------------------
# Mean-Variance Optimizer
# ---------------------------------------------------------------------------


class TestMeanVarianceOptimizer:
    def test_mvo_weights_sum_to_one(self):
        """Optimized weights should sum to approximately 1."""
        returns = _make_returns()
        mu, cov = _expected_returns_and_cov(returns)
        optimizer = MeanVarianceOptimizer(target="max_sharpe")

        weights = optimizer.optimize(mu, cov)
        total = sum(weights.values())
        assert abs(total - 1.0) < 0.01

    def test_mvo_all_positive_weights(self):
        """With min_weight=0, all weights should be non-negative."""
        returns = _make_returns()
        mu, cov = _expected_returns_and_cov(returns)
        optimizer = MeanVarianceOptimizer(target="max_sharpe")

        weights = optimizer.optimize(mu, cov, constraints={"min_weight": 0.0})
        assert all(w >= -1e-6 for w in weights.values())

    def test_mvo_max_weight_constraint(self):
        """No single asset should exceed the max_weight constraint."""
        returns = _make_returns()
        mu, cov = _expected_returns_and_cov(returns)
        optimizer = MeanVarianceOptimizer(target="max_sharpe")

        max_w = 0.4
        weights = optimizer.optimize(mu, cov, constraints={"max_weight": max_w})
        assert all(w <= max_w + 0.01 for w in weights.values())

    def test_mvo_min_vol(self):
        """Min-vol portfolio should have lower vol than equal-weight."""
        returns = _make_returns()
        mu, cov = _expected_returns_and_cov(returns)
        optimizer = MeanVarianceOptimizer(target="min_vol")

        weights = optimizer.optimize(mu, cov)

        # Compute portfolio vol
        w = np.array([weights.get(s, 0) for s in mu.index])
        port_vol = np.sqrt(w @ cov.values @ w)

        # Equal weight vol
        ew = np.ones(len(mu)) / len(mu)
        ew_vol = np.sqrt(ew @ cov.values @ ew)

        assert port_vol <= ew_vol + 0.01


# ---------------------------------------------------------------------------
# Risk Parity
# ---------------------------------------------------------------------------


class TestRiskParityOptimizer:
    def test_risk_parity_weights_sum_to_one(self):
        """Risk parity weights should sum to approximately 1."""
        returns = _make_returns()
        mu, cov = _expected_returns_and_cov(returns)
        optimizer = RiskParityOptimizer(method="inverse_vol")

        weights = optimizer.optimize(mu, cov)
        total = sum(weights.values())
        assert abs(total - 1.0) < 0.01

    def test_risk_parity_positive_weights(self):
        """Risk parity weights should all be positive."""
        returns = _make_returns()
        mu, cov = _expected_returns_and_cov(returns)
        optimizer = RiskParityOptimizer(method="inverse_vol")

        weights = optimizer.optimize(mu, cov)
        assert all(w > 0 for w in weights.values())


# ---------------------------------------------------------------------------
# Kelly Criterion
# ---------------------------------------------------------------------------


class TestKellyCriterion:
    def test_kelly_weights_bounded(self):
        """Kelly weights should be bounded by clip range (-0.5, 0.5)."""
        returns = _make_returns()
        mu, cov = _expected_returns_and_cov(returns)
        kelly = KellyCriterion(fraction=0.5)

        weights = kelly.optimize(mu, cov)
        assert all(abs(w) <= 0.5 + 1e-6 for w in weights.values())

    def test_kelly_single_asset(self):
        """Single-asset Kelly with positive edge should return positive fraction."""
        f = KellyCriterion.single_asset_kelly(
            win_prob=0.55, win_loss_ratio=2.0, fraction=1.0
        )
        assert f > 0

    def test_kelly_no_edge(self):
        """With no edge, Kelly fraction should be zero."""
        f = KellyCriterion.single_asset_kelly(
            win_prob=0.30, win_loss_ratio=1.0, fraction=1.0
        )
        assert f == 0  # max(0, negative) = 0


# ---------------------------------------------------------------------------
# Weight Normalization
# ---------------------------------------------------------------------------


class TestNormalizeWeights:
    def test_normalize_basic(self):
        """Normalized weights should sum to 1."""
        weights = {"A": 0.3, "B": 0.5, "C": 0.7}
        normed = PortfolioOptimizer.normalize_weights(weights)
        assert abs(sum(normed.values()) - 1.0) < 1e-6

    def test_normalize_preserves_ratio(self):
        """Normalization should preserve relative ratios."""
        weights = {"A": 2.0, "B": 1.0}
        normed = PortfolioOptimizer.normalize_weights(weights)
        assert abs(normed["A"] / normed["B"] - 2.0) < 1e-6

    def test_normalize_empty(self):
        """Normalizing empty weights should return empty dict."""
        assert PortfolioOptimizer.normalize_weights({}) == {}
