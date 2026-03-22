"""Unit tests for backtesting metrics and vectorized backtest engine."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtesting.metrics.performance import compute_metrics
from backtesting.metrics.risk_metrics import compute_risk_metrics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_equity_curve(
    initial: float = 100_000,
    n_days: int = 252,
    drift: float = 0.0003,
    vol: float = 0.01,
    seed: int = 42,
) -> pd.DataFrame:
    """Generate a synthetic equity curve with controlled drift and volatility."""
    rng = np.random.RandomState(seed)
    returns = drift + vol * rng.randn(n_days)
    prices = initial * np.cumprod(1 + returns)
    prices = np.insert(prices, 0, initial)
    returns = np.insert(returns, 0, 0.0)
    dates = pd.date_range("2024-01-01", periods=len(prices), freq="B")
    return pd.DataFrame(
        {"total_value": prices, "returns": returns},
        index=dates,
    )


def _make_drawdown_curve() -> pd.DataFrame:
    """Equity curve with a known drawdown pattern."""
    values = [100_000, 110_000, 120_000, 105_000, 95_000, 100_000, 115_000]
    returns = [0] + [
        (values[i] - values[i - 1]) / values[i - 1] for i in range(1, len(values))
    ]
    dates = pd.date_range("2024-01-01", periods=len(values), freq="B")
    return pd.DataFrame(
        {"total_value": values, "returns": returns},
        index=dates,
    )


# ---------------------------------------------------------------------------
# Performance Metrics
# ---------------------------------------------------------------------------


class TestPerformanceMetrics:
    def test_compute_metrics_basic(self):
        """compute_metrics should return all expected keys."""
        df = _make_equity_curve()
        metrics = compute_metrics(df)

        expected_keys = [
            "total_return", "annual_return", "sharpe_ratio", "sortino_ratio",
            "calmar_ratio", "max_drawdown", "win_rate", "profit_factor",
        ]
        for key in expected_keys:
            assert key in metrics, f"Missing metric: {key}"

    def test_positive_drift_positive_return(self):
        """Positive drift should produce positive total return."""
        df = _make_equity_curve(drift=0.001, vol=0.005)
        metrics = compute_metrics(df)
        assert metrics["total_return"] > 0
        assert metrics["sharpe_ratio"] > 0

    def test_negative_drift_negative_return(self):
        """Negative drift should produce negative total return."""
        df = _make_equity_curve(drift=-0.001, vol=0.005)
        metrics = compute_metrics(df)
        assert metrics["total_return"] < 0

    def test_zero_vol_sharpe(self):
        """With zero volatility, Sharpe should be 0 (avoid division by zero)."""
        prices = np.linspace(100_000, 100_000, 20)  # flat
        dates = pd.date_range("2024-01-01", periods=20, freq="B")
        df = pd.DataFrame({"total_value": prices}, index=dates)
        metrics = compute_metrics(df)
        assert metrics["sharpe_ratio"] == 0

    def test_max_drawdown(self):
        """Max drawdown should match the known drawdown in the test curve."""
        df = _make_drawdown_curve()
        metrics = compute_metrics(df)
        # Peak = 120k, trough = 95k => dd = 25k/120k ≈ 20.83%
        assert abs(metrics["max_drawdown"] - 0.2083) < 0.01

    def test_win_rate(self):
        """Win rate should be the fraction of positive return periods."""
        df = _make_equity_curve(seed=99)
        metrics = compute_metrics(df)
        assert 0 <= metrics["win_rate"] <= 1

    def test_empty_dataframe(self):
        """Empty DataFrame should return empty dict."""
        df = pd.DataFrame()
        metrics = compute_metrics(df)
        assert metrics == {}


# ---------------------------------------------------------------------------
# Risk Metrics
# ---------------------------------------------------------------------------


class TestRiskMetrics:
    def test_risk_metrics_keys(self):
        """compute_risk_metrics should return VaR and CVaR."""
        df = _make_equity_curve()
        metrics = compute_risk_metrics(df["returns"].dropna())

        assert "var_95_daily" in metrics
        assert "cvar_95_daily" in metrics

    def test_var_is_positive(self):
        """VaR should be positive for a portfolio with volatility."""
        df = _make_equity_curve(vol=0.02)
        metrics = compute_risk_metrics(df["returns"].dropna())
        assert metrics["var_95_daily"] > 0

    def test_cvar_geq_var(self):
        """CVaR (expected shortfall) should be >= VaR."""
        df = _make_equity_curve(vol=0.02, seed=7)
        metrics = compute_risk_metrics(df["returns"].dropna())
        assert metrics["cvar_95_daily"] >= metrics["var_95_daily"]

    def test_risk_metrics_with_low_vol(self):
        """Low volatility should produce small VaR."""
        df = _make_equity_curve(vol=0.001)
        metrics = compute_risk_metrics(df["returns"].dropna())
        assert metrics["var_95_daily"] < 0.01
