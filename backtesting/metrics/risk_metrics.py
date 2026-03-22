"""Risk metrics for backtesting: VaR, CVaR, tail risk analysis."""

from __future__ import annotations

from typing import Any, Dict

import numpy as np
import pandas as pd


def compute_risk_metrics(
    returns: pd.Series,
    confidence: float = 0.95,
    portfolio_value: float = 100_000,
) -> Dict[str, Any]:
    """Compute risk metrics from a returns series."""
    if len(returns) < 10:
        return {}

    sorted_returns = np.sort(returns.values)
    n = len(sorted_returns)

    # Historical VaR
    var_idx = int((1 - confidence) * n)
    var_daily = -sorted_returns[var_idx]
    var_dollar = var_daily * portfolio_value

    # CVaR (Expected Shortfall)
    cvar_daily = -np.mean(sorted_returns[:var_idx + 1]) if var_idx > 0 else var_daily
    cvar_dollar = cvar_daily * portfolio_value

    # Tail ratio
    percentile_95 = np.percentile(returns, 95)
    percentile_5 = abs(np.percentile(returns, 5))
    tail_ratio = percentile_95 / percentile_5 if percentile_5 > 0 else 0

    # Skewness and Kurtosis
    skew = float(returns.skew())
    kurtosis = float(returns.kurtosis())

    return {
        "var_95_daily": float(var_daily),
        "var_95_dollar": float(var_dollar),
        "cvar_95_daily": float(cvar_daily),
        "cvar_95_dollar": float(cvar_dollar),
        "tail_ratio": float(tail_ratio),
        "skewness": skew,
        "kurtosis": kurtosis,
        "worst_day": float(sorted_returns[0]),
        "best_day": float(sorted_returns[-1]),
    }
