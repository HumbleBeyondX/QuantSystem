"""Unified transaction cost model for mid-frequency crypto strategies.

Combines fee estimation, orderbook-depth-aware slippage estimation,
and profitability gating into a single model. This is the most critical
component for retail traders — without it, strategies bleed fees on
marginal signals.

Usage:
    cost = CostModel(maker_fee=0.0002, taker_fee=0.0004)
    rt_cost = cost.estimate_round_trip_cost_bps(price=50000, qty=0.1, ob_depth=5.0)
    if cost.is_trade_profitable(expected_move_bps=15, qty=0.1, price=50000, ob_depth=5.0):
        # Place order
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np


@dataclass
class CostBreakdown:
    """Detailed breakdown of estimated transaction costs."""

    fee_bps: float          # Exchange fee component (round-trip)
    slippage_bps: float     # Estimated slippage component (round-trip)
    total_bps: float        # Total round-trip cost
    fee_usd: float          # Absolute fee in USD
    slippage_usd: float     # Absolute slippage in USD
    total_usd: float        # Total cost in USD


class CostModel:
    """Unified transaction cost estimator for crypto mid-frequency strategies.

    Estimates the all-in cost of a round-trip trade (entry + exit) including
    exchange fees and orderbook-depth-aware slippage. Provides a profitability
    gate to reject trades where expected profit doesn't justify the cost.

    Key principle: A retail trader must demand expected profit >= 2x costs
    to maintain positive expectancy after accounting for model uncertainty.
    """

    def __init__(
        self,
        maker_fee: float = 0.0002,      # 0.02% (Binance VIP0 maker)
        taker_fee: float = 0.0004,      # 0.04% (Binance VIP0 taker)
        base_slippage_bps: float = 1.0,  # Minimum slippage even in deep books
        depth_impact_factor: float = 0.05,  # How much qty/depth ratio affects slippage
        min_profit_to_cost_ratio: float = 2.0,  # Must expect 2x cost to trade
    ) -> None:
        self.maker_fee = maker_fee
        self.taker_fee = taker_fee
        self.base_slippage_bps = base_slippage_bps
        self.depth_impact_factor = depth_impact_factor
        self.min_profit_to_cost_ratio = min_profit_to_cost_ratio

    def estimate_slippage_bps(
        self,
        quantity: float,
        price: float,
        orderbook_depth: float,
    ) -> float:
        """Estimate one-way slippage in basis points.

        Slippage model: base_slippage + impact_factor * (order_value / depth_value)^0.5

        The square root reflects empirical findings that market impact scales
        sub-linearly with order size (Kyle's lambda model).

        Args:
            quantity: Order size in base asset units.
            price: Current price.
            orderbook_depth: Available liquidity within ~0.1% of mid (in base units).

        Returns:
            Estimated one-way slippage in basis points.
        """
        if orderbook_depth <= 0 or price <= 0:
            return self.base_slippage_bps * 3  # Penalize unknown depth

        order_value = quantity * price
        depth_value = orderbook_depth * price

        # Sub-linear market impact
        impact_ratio = order_value / depth_value if depth_value > 0 else 1.0
        impact_bps = self.depth_impact_factor * (impact_ratio ** 0.5) * 10_000

        return self.base_slippage_bps + impact_bps

    def estimate_fee_bps(self, is_maker: bool = False) -> float:
        """Estimate one-way fee in basis points.

        Args:
            is_maker: Whether the order is expected to be a maker order.

        Returns:
            Fee in basis points.
        """
        rate = self.maker_fee if is_maker else self.taker_fee
        return rate * 10_000

    def estimate_round_trip_cost(
        self,
        price: float,
        quantity: float,
        orderbook_depth: float,
        entry_is_maker: bool = False,
        exit_is_maker: bool = False,
    ) -> CostBreakdown:
        """Estimate full round-trip cost (entry + exit).

        Conservative assumption: entry as taker (crossing the spread),
        exit also as taker (most mid-freq strategies need immediate exit).

        Args:
            price: Current mid price.
            quantity: Position size in base units.
            orderbook_depth: Available depth near mid (base units).
            entry_is_maker: If True, assume maker fee for entry.
            exit_is_maker: If True, assume maker fee for exit.

        Returns:
            CostBreakdown with detailed fee/slippage components.
        """
        notional = price * quantity

        # Fees
        entry_fee_bps = self.estimate_fee_bps(is_maker=entry_is_maker)
        exit_fee_bps = self.estimate_fee_bps(is_maker=exit_is_maker)
        total_fee_bps = entry_fee_bps + exit_fee_bps

        # Slippage (both entry and exit)
        entry_slip = self.estimate_slippage_bps(quantity, price, orderbook_depth)
        exit_slip = self.estimate_slippage_bps(quantity, price, orderbook_depth)
        total_slip_bps = entry_slip + exit_slip

        total_bps = total_fee_bps + total_slip_bps
        fee_usd = notional * total_fee_bps / 10_000
        slip_usd = notional * total_slip_bps / 10_000

        return CostBreakdown(
            fee_bps=total_fee_bps,
            slippage_bps=total_slip_bps,
            total_bps=total_bps,
            fee_usd=fee_usd,
            slippage_usd=slip_usd,
            total_usd=fee_usd + slip_usd,
        )

    def estimate_round_trip_cost_bps(
        self,
        price: float,
        quantity: float,
        orderbook_depth: float,
    ) -> float:
        """Quick estimate of round-trip cost in basis points.

        Uses conservative assumption (taker both sides).
        """
        breakdown = self.estimate_round_trip_cost(price, quantity, orderbook_depth)
        return breakdown.total_bps

    def is_trade_profitable(
        self,
        expected_move_bps: float,
        quantity: float,
        price: float,
        orderbook_depth: float,
    ) -> bool:
        """Check if expected profit exceeds minimum cost threshold.

        Returns True only if:
            expected_move_bps >= min_profit_to_cost_ratio * round_trip_cost_bps

        This is the critical profitability gate. A retail trader must demand
        higher expected returns to compensate for:
        - Model uncertainty (factor signal isn't always correct)
        - Execution risk (slippage may exceed estimate)
        - Opportunity cost (capital tied up during the trade)

        Args:
            expected_move_bps: Expected price move in basis points.
            quantity: Planned position size in base units.
            price: Current price.
            orderbook_depth: Available depth near mid (base units).

        Returns:
            True if trade clears the profitability hurdle.
        """
        rt_cost = self.estimate_round_trip_cost_bps(price, quantity, orderbook_depth)
        return expected_move_bps >= self.min_profit_to_cost_ratio * rt_cost

    def optimal_quantity(
        self,
        signal_strength: float,
        max_position_usd: float,
        price: float,
        orderbook_depth: float,
        max_slippage_bps: float = 5.0,
    ) -> float:
        """Compute optimal position size based on signal strength and liquidity.

        Stronger signals → larger positions, but capped by:
        1. max_position_usd * abs(signal_strength) — signal-scaled sizing
        2. Quantity where slippage would exceed max_slippage_bps — depth cap

        Args:
            signal_strength: Composite signal in [-1, 1].
            max_position_usd: Maximum position value in USD.
            price: Current price.
            orderbook_depth: Available depth near mid (base units).
            max_slippage_bps: Maximum acceptable one-way slippage.

        Returns:
            Optimal quantity in base units.
        """
        if price <= 0 or abs(signal_strength) < 1e-6:
            return 0.0

        # Signal-scaled quantity
        target_usd = max_position_usd * abs(signal_strength)
        target_qty = target_usd / price

        # Cap by depth: find max qty where slippage <= max_slippage_bps
        if orderbook_depth > 0:
            # From slippage formula: max_slip >= base + factor * sqrt(qty / depth) * 10000
            # Solve for qty: qty <= depth * ((max_slip - base) / (factor * 10000))^2
            slip_budget = max_slippage_bps - self.base_slippage_bps
            if slip_budget > 0 and self.depth_impact_factor > 0:
                max_ratio = (slip_budget / (self.depth_impact_factor * 10_000)) ** 2
                depth_cap_qty = orderbook_depth * max_ratio
                target_qty = min(target_qty, depth_cap_qty)

        return max(0.0, target_qty)
