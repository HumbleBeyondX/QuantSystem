"""Wrappers for the existing MeanVarianceOptimizer that fit the
sector-rotation pipeline (signal-tilted expected returns + SPY hedge).

Reuses ``portfolio/optimization/mean_variance.py``. The two functions
here are convenience adapters specific to this strategy:

  optimize_long_sleeve()  — turn (expected_returns, cov, constraints)
                            into long-only weights summing to 1.
  compute_spy_hedge()     — given long weights and per-ticker betas,
                            compute the SPY short notional that
                            beta-neutralizes the portfolio.
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd


def optimize_long_sleeve(
    expected_returns: pd.Series,
    cov_matrix: pd.DataFrame,
    *,
    weight_min: float = 0.0,
    weight_max: float = 0.30,
    target_volatility: Optional[float] = 0.08,
    risk_free_rate: float = 0.04,
) -> Dict[str, float]:
    """Compute long-only MV weights summing to 1.

    Tries PyPortfolioOpt's EfficientFrontier in this order:
      1. efficient_risk(target_volatility) — match a target vol
      2. max_sharpe — fallback if vol target infeasible
      3. equal-weight — final fallback if optimizer fails

    Args:
        expected_returns: pd.Series indexed by ticker (annualized).
        cov_matrix: pd.DataFrame indexed by ticker (annualized).
        weight_min, weight_max: per-ticker bounds.
        target_volatility: annualized portfolio vol target.
        risk_free_rate: for Sharpe calc.
    """
    # Align tickers: keep only those in BOTH inputs and which have
    # non-NaN expected returns
    common = expected_returns.dropna().index.intersection(cov_matrix.index)
    if len(common) == 0:
        return {}
    er = expected_returns.loc[common]
    cov = cov_matrix.loc[common, common]

    n = len(er)
    if n == 1:
        return {er.index[0]: 1.0}

    try:
        from pypfopt import EfficientFrontier
        ef = EfficientFrontier(er, cov, weight_bounds=(weight_min, weight_max))

        if target_volatility is not None:
            try:
                ef.efficient_risk(target_volatility)
            except Exception:
                # Target infeasible (often when cov is too high) — fallback
                ef = EfficientFrontier(er, cov, weight_bounds=(weight_min, weight_max))
                ef.max_sharpe(risk_free_rate=risk_free_rate)
        else:
            ef.max_sharpe(risk_free_rate=risk_free_rate)

        weights = ef.clean_weights()
        return {t: float(w) for t, w in weights.items()}
    except Exception:
        # Final fallback: equal weight, capped at weight_max
        per = min(1.0 / n, weight_max)
        return {t: per for t in er.index}


def compute_portfolio_beta(
    weights: Dict[str, float],
    betas: pd.Series,
) -> float:
    """Weighted-average beta of the portfolio."""
    s = 0.0
    for ticker, w in weights.items():
        if ticker in betas.index and not pd.isna(betas[ticker]):
            s += w * float(betas[ticker])
    return s


def compute_spy_hedge(
    long_weights: Dict[str, float],
    long_dollar_size: float,
    portfolio_beta: float,
    *,
    max_short_pct: float = 1.5,
) -> float:
    """Compute the dollar amount of SPY to SHORT for beta-neutralization.

    The hedge target: shorting `long_dollar_size × portfolio_beta` of SPY
    so the combined book has approx zero market beta.

    Args:
        long_weights: dict of long-sleeve weights (already MV-optimized).
        long_dollar_size: total $ deployed in the long sleeve.
        portfolio_beta: weighted average beta of the long sleeve vs SPY.
        max_short_pct: cap the short notional at this fraction of long
                       sleeve (e.g. 1.5 means short ≤ 150% of long).

    Returns:
        Positive float: dollars of SPY to short. Use the negative of this
        as the SPY weight in the final book.
    """
    if portfolio_beta <= 0:
        return 0.0  # already low/negative beta — no hedge needed
    raw_short = long_dollar_size * portfolio_beta
    cap = long_dollar_size * max_short_pct
    return min(raw_short, cap)
