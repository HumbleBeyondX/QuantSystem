"""Unit tests for options Greeks calculator and vol surface."""

from __future__ import annotations

import math

import pytest

from strategies.options.greeks_calculator import (
    GreeksResult,
    black_scholes_greeks,
    compute_hedge_quantity,
    compute_implied_vol,
    compute_portfolio_delta,
)
from core.models import OptionContract


# ---------------------------------------------------------------------------
# Black-Scholes Greeks
# ---------------------------------------------------------------------------


class TestBlackScholesGreeks:
    def test_call_delta_between_0_and_1(self):
        """Call delta should be between 0 and 1."""
        result = black_scholes_greeks(
            spot=100, strike=100, tte=0.25, rate=0.05, vol=0.20, is_call=True
        )
        assert 0 < result.delta < 1

    def test_put_delta_between_neg1_and_0(self):
        """Put delta should be between -1 and 0."""
        result = black_scholes_greeks(
            spot=100, strike=100, tte=0.25, rate=0.05, vol=0.20, is_call=False
        )
        assert -1 < result.delta < 0

    def test_atm_call_delta_near_half(self):
        """ATM call delta should be approximately 0.5."""
        result = black_scholes_greeks(
            spot=100, strike=100, tte=0.25, rate=0.05, vol=0.20, is_call=True
        )
        assert abs(result.delta - 0.55) < 0.10  # ATM with drift slightly > 0.5

    def test_deep_itm_call_delta_near_one(self):
        """Deep ITM call should have delta near 1."""
        result = black_scholes_greeks(
            spot=150, strike=100, tte=0.25, rate=0.05, vol=0.20, is_call=True
        )
        assert result.delta > 0.95

    def test_deep_otm_call_delta_near_zero(self):
        """Deep OTM call should have delta near 0."""
        result = black_scholes_greeks(
            spot=50, strike=100, tte=0.25, rate=0.05, vol=0.20, is_call=True
        )
        assert result.delta < 0.05

    def test_gamma_positive(self):
        """Gamma should be positive for both calls and puts."""
        for is_call in [True, False]:
            result = black_scholes_greeks(
                spot=100, strike=100, tte=0.25, rate=0.05, vol=0.20, is_call=is_call
            )
            assert result.gamma > 0

    def test_vega_positive(self):
        """Vega should be positive for both calls and puts."""
        for is_call in [True, False]:
            result = black_scholes_greeks(
                spot=100, strike=100, tte=0.25, rate=0.05, vol=0.20, is_call=is_call
            )
            assert result.vega > 0

    def test_theta_negative_for_long(self):
        """Theta should be negative (time decay) for ATM options."""
        result = black_scholes_greeks(
            spot=100, strike=100, tte=0.25, rate=0.05, vol=0.20, is_call=True
        )
        assert result.theta < 0

    def test_put_call_parity(self):
        """Put-call parity: C - P = S*e^(-qT) - K*e^(-rT)."""
        S, K, T, r, vol, q = 100, 100, 0.25, 0.05, 0.20, 0.0
        call = black_scholes_greeks(S, K, T, r, vol, True, q)
        put = black_scholes_greeks(S, K, T, r, vol, False, q)

        parity_lhs = call.theoretical_price - put.theoretical_price
        parity_rhs = S * math.exp(-q * T) - K * math.exp(-r * T)
        assert abs(parity_lhs - parity_rhs) < 0.01

    def test_expired_option_returns_zeros(self):
        """Options at expiry (tte=0) should have zero Greeks."""
        result = black_scholes_greeks(
            spot=100, strike=100, tte=0, rate=0.05, vol=0.20, is_call=True
        )
        assert result.delta == 0
        assert result.gamma == 0
        assert result.theoretical_price == 0


# ---------------------------------------------------------------------------
# Implied Volatility
# ---------------------------------------------------------------------------


class TestImpliedVol:
    def test_iv_round_trip(self):
        """Computing BS price then solving for IV should return the original vol."""
        spot, strike, tte, rate, vol = 100, 105, 0.5, 0.05, 0.25
        price = black_scholes_greeks(spot, strike, tte, rate, vol, True).theoretical_price
        recovered_vol = compute_implied_vol(price, spot, strike, tte, rate, True)
        assert abs(recovered_vol - vol) < 0.001

    def test_iv_for_puts(self):
        """IV solver should work for puts."""
        spot, strike, tte, rate, vol = 100, 95, 0.3, 0.04, 0.30
        price = black_scholes_greeks(spot, strike, tte, rate, vol, False).theoretical_price
        recovered_vol = compute_implied_vol(price, spot, strike, tte, rate, False)
        assert abs(recovered_vol - vol) < 0.001

    def test_iv_zero_price_returns_zero(self):
        """Zero market price should return 0 IV."""
        assert compute_implied_vol(0, 100, 100, 0.25, 0.05, True) == 0.0


# ---------------------------------------------------------------------------
# Portfolio Delta and Hedging
# ---------------------------------------------------------------------------


class TestPortfolioDelta:
    def _make_option(self, delta: float, multiplier: int = 100) -> OptionContract:
        from datetime import datetime
        return OptionContract(
            symbol="AAPL240621C00190000",
            underlying="AAPL",
            option_type="CALL",
            strike=190.0,
            expiry=datetime(2024, 6, 21),
            multiplier=multiplier,
            delta=delta,
        )

    def test_single_option_delta(self):
        """Portfolio delta of one contract should be delta * multiplier."""
        opt = self._make_option(delta=0.50)
        assert compute_portfolio_delta([opt]) == 50.0

    def test_hedge_quantity(self):
        """Hedge quantity should offset portfolio delta."""
        opt = self._make_option(delta=0.50)
        hedge = compute_hedge_quantity([opt], stock_position=0)
        assert hedge == -50.0  # sell 50 shares to hedge

    def test_already_hedged(self):
        """If stock offsets delta, hedge quantity should be near zero."""
        opt = self._make_option(delta=0.50)
        hedge = compute_hedge_quantity([opt], stock_position=-50.0)
        assert abs(hedge) < 0.01
