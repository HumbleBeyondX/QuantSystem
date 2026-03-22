"""Options Greeks calculator using Black-Scholes and QuantLib.

Provides delta, gamma, vega, theta, rho computation for individual options
and vol surface construction. Falls back to analytical BS when QuantLib unavailable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.stats import norm

from core.logging import get_logger
from core.models import OptionContract

logger = get_logger(__name__)

# Try QuantLib, fall back to analytical BS
try:
    import QuantLib as ql
    HAS_QUANTLIB = True
except ImportError:
    HAS_QUANTLIB = False
    logger.warning("QuantLib not installed, using analytical Black-Scholes")


@dataclass
class GreeksResult:
    """Greeks computation result for a single option."""
    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float
    implied_vol: float
    theoretical_price: float


def black_scholes_greeks(
    spot: float,
    strike: float,
    tte: float,       # Time to expiry in years
    rate: float,       # Risk-free rate (annual)
    vol: float,        # Implied volatility (annual)
    is_call: bool,
    dividend_yield: float = 0.0,
) -> GreeksResult:
    """Compute Greeks using analytical Black-Scholes."""
    if tte <= 0 or vol <= 0 or spot <= 0:
        return GreeksResult(0, 0, 0, 0, 0, vol, 0)

    sqrt_t = math.sqrt(tte)
    d1 = (math.log(spot / strike) + (rate - dividend_yield + 0.5 * vol ** 2) * tte) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t

    # Standard normal PDF and CDF
    nd1 = norm.cdf(d1)
    nd2 = norm.cdf(d2)
    n_neg_d1 = norm.cdf(-d1)
    n_neg_d2 = norm.cdf(-d2)
    pdf_d1 = norm.pdf(d1)

    exp_div = math.exp(-dividend_yield * tte)
    exp_rate = math.exp(-rate * tte)

    if is_call:
        price = spot * exp_div * nd1 - strike * exp_rate * nd2
        delta = exp_div * nd1
        theta = (
            -spot * exp_div * pdf_d1 * vol / (2 * sqrt_t)
            - rate * strike * exp_rate * nd2
            + dividend_yield * spot * exp_div * nd1
        ) / 365  # Daily theta
        rho = strike * tte * exp_rate * nd2 / 100
    else:
        price = strike * exp_rate * n_neg_d2 - spot * exp_div * n_neg_d1
        delta = -exp_div * n_neg_d1
        theta = (
            -spot * exp_div * pdf_d1 * vol / (2 * sqrt_t)
            + rate * strike * exp_rate * n_neg_d2
            - dividend_yield * spot * exp_div * n_neg_d1
        ) / 365
        rho = -strike * tte * exp_rate * n_neg_d2 / 100

    gamma = exp_div * pdf_d1 / (spot * vol * sqrt_t)
    vega = spot * exp_div * pdf_d1 * sqrt_t / 100  # Per 1% vol change

    return GreeksResult(
        delta=delta,
        gamma=gamma,
        vega=vega,
        theta=theta,
        rho=rho,
        implied_vol=vol,
        theoretical_price=price,
    )


def compute_implied_vol(
    market_price: float,
    spot: float,
    strike: float,
    tte: float,
    rate: float,
    is_call: bool,
    dividend_yield: float = 0.0,
    max_iterations: int = 100,
    precision: float = 1e-6,
) -> float:
    """Newton-Raphson implied volatility solver."""
    if market_price <= 0 or tte <= 0:
        return 0.0

    # Initial guess
    vol = 0.3

    for _ in range(max_iterations):
        result = black_scholes_greeks(spot, strike, tte, rate, vol, is_call, dividend_yield)
        diff = result.theoretical_price - market_price
        if abs(diff) < precision:
            return vol

        # Newton step: vega is dPrice/dVol
        vega_raw = result.vega * 100  # Undo the /100 scaling
        if abs(vega_raw) < 1e-10:
            break

        vol -= diff / vega_raw
        vol = max(0.01, min(vol, 5.0))  # Clamp

    return vol


def update_option_greeks(
    contract: OptionContract,
    spot: float,
    rate: float = 0.05,
    dividend_yield: float = 0.0,
) -> OptionContract:
    """Compute and update Greeks on an OptionContract in-place."""
    is_call = contract.option_type.upper() == "CALL"
    tte = contract.days_to_expiry / 365.0

    # Get IV from market if available, otherwise compute
    iv = contract.implied_vol
    if iv <= 0 and contract.mid_price > 0:
        iv = compute_implied_vol(
            contract.mid_price, spot, contract.strike, tte, rate, is_call, dividend_yield
        )

    if iv <= 0:
        return contract

    result = black_scholes_greeks(spot, contract.strike, tte, rate, iv, is_call, dividend_yield)

    contract.delta = result.delta
    contract.gamma = result.gamma
    contract.vega = result.vega
    contract.theta = result.theta
    contract.rho = result.rho
    contract.implied_vol = iv

    return contract


def compute_portfolio_delta(
    options: List[OptionContract],
    stock_position: float = 0.0,
) -> float:
    """Compute aggregate portfolio delta."""
    options_delta = sum(opt.delta * opt.multiplier for opt in options)
    return options_delta + stock_position


def compute_hedge_quantity(
    options: List[OptionContract],
    stock_position: float = 0.0,
) -> float:
    """Compute required stock hedge to achieve delta-neutral."""
    portfolio_delta = compute_portfolio_delta(options, stock_position)
    return -portfolio_delta  # Negate to offset
