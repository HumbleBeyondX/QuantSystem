"""Financial math helper functions."""

from __future__ import annotations

import math
from typing import List, Optional, Sequence

import numpy as np


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Safe division that returns default on zero denominator."""
    if denominator == 0:
        return default
    return numerator / denominator


def pct_change(old: float, new: float) -> float:
    """Calculate percentage change."""
    return safe_divide(new - old, abs(old)) * 100


def sharpe_ratio(
    returns: Sequence[float],
    risk_free_rate: float = 0.0,
    annualization_factor: float = 252,
) -> float:
    """Annualized Sharpe ratio."""
    arr = np.array(returns)
    if len(arr) < 2:
        return 0.0
    excess = arr - risk_free_rate / annualization_factor
    std = np.std(excess, ddof=1)
    if std == 0:
        return 0.0
    return float(np.mean(excess) / std * np.sqrt(annualization_factor))


def sortino_ratio(
    returns: Sequence[float],
    risk_free_rate: float = 0.0,
    annualization_factor: float = 252,
) -> float:
    """Annualized Sortino ratio (downside risk only)."""
    arr = np.array(returns)
    if len(arr) < 2:
        return 0.0
    excess = arr - risk_free_rate / annualization_factor
    downside = np.minimum(excess, 0)
    downside_std = np.std(downside, ddof=1)
    if downside_std == 0:
        return 0.0
    return float(np.mean(excess) / downside_std * np.sqrt(annualization_factor))


def max_drawdown(equity_curve: Sequence[float]) -> float:
    """Maximum drawdown as a positive fraction (e.g., 0.15 = 15% drawdown)."""
    arr = np.array(equity_curve)
    if len(arr) < 2:
        return 0.0
    peak = np.maximum.accumulate(arr)
    drawdown = (peak - arr) / peak
    return float(np.max(drawdown))


def calmar_ratio(
    returns: Sequence[float],
    equity_curve: Sequence[float],
    annualization_factor: float = 252,
) -> float:
    """Calmar ratio: annualized return / max drawdown."""
    ann_return = float(np.mean(returns)) * annualization_factor
    mdd = max_drawdown(equity_curve)
    return safe_divide(ann_return, mdd)


def win_rate(pnls: Sequence[float]) -> float:
    """Percentage of profitable trades."""
    arr = np.array(pnls)
    if len(arr) == 0:
        return 0.0
    return float(np.sum(arr > 0) / len(arr))


def profit_factor(pnls: Sequence[float]) -> float:
    """Sum of profits / sum of losses."""
    arr = np.array(pnls)
    gains = np.sum(arr[arr > 0])
    losses = abs(np.sum(arr[arr < 0]))
    return safe_divide(gains, losses)


def information_coefficient(factor_values: Sequence[float], returns: Sequence[float]) -> float:
    """Spearman rank correlation between factor and forward returns."""
    from scipy.stats import spearmanr

    if len(factor_values) < 3:
        return 0.0
    corr, _ = spearmanr(factor_values, returns)
    return float(corr) if not math.isnan(corr) else 0.0


def value_at_risk(
    returns: Sequence[float], confidence: float = 0.99, method: str = "historical"
) -> float:
    """Value at Risk at given confidence level (as positive number)."""
    arr = np.array(returns)
    if len(arr) < 10:
        return 0.0
    if method == "historical":
        return float(-np.percentile(arr, (1 - confidence) * 100))
    elif method == "parametric":
        from scipy.stats import norm

        mu = np.mean(arr)
        sigma = np.std(arr, ddof=1)
        return float(-(mu + norm.ppf(1 - confidence) * sigma))
    return 0.0


def conditional_var(returns: Sequence[float], confidence: float = 0.99) -> float:
    """Conditional VaR (Expected Shortfall) at given confidence level."""
    arr = np.array(returns)
    if len(arr) < 10:
        return 0.0
    var = value_at_risk(returns, confidence)
    tail = arr[arr <= -var]
    if len(tail) == 0:
        return var
    return float(-np.mean(tail))
