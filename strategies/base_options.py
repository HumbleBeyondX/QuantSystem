"""Options strategy base class — Greeks-aware with volatility surface integration.

Extends StrategyBase with options-specific capabilities:
- Real-time Greeks tracking (delta, gamma, vega, theta, rho)
- Portfolio-level Greeks aggregation and neutrality targets
- Volatility surface access (ATM vol, skew, term structure)
- Multi-leg position management (spreads, straddles, iron condors, etc.)
- Automatic delta-hedging infrastructure
- Expiry tracking and roll management
- Options-specific risk limits (max vega, max gamma, max theta)

Usage:
    class MyOptionsStrategy(OptionsStrategyBase):
        def generate_signals(self) -> List[Signal]:
            iv = self.get_atm_iv("SPY")
            rv = self.realized_vol("SPY", 20)
            if iv - rv > 0.04:  # VRP > 4 points
                return [self.sell_straddle_signal("SPY", ...)]
            return []
"""

from __future__ import annotations

import math
from abc import abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, List, Optional, Tuple
from uuid import uuid4

import numpy as np

from core.enums import AssetClass, SignalDirection
from core.models import Bar, Fill, OptionContract, GreeksExposure, Signal, TargetPosition
from strategies.base import StrategyBase, StrategyConfig


# ------------------------------------------------------------------
# Option Leg & Multi-Leg structures
# ------------------------------------------------------------------

@dataclass
class OptionLeg:
    """Single leg of an options position."""
    symbol: str           # Option symbol (e.g., SPY260320C550)
    underlying: str       # Underlying symbol (e.g., SPY)
    strike: float
    expiry: datetime
    option_type: str      # "CALL" or "PUT"
    quantity: int          # Positive = long, negative = short
    entry_price: float = 0.0
    current_price: float = 0.0

    # Greeks (updated in real-time)
    delta: float = 0.0
    gamma: float = 0.0
    vega: float = 0.0
    theta: float = 0.0

    @property
    def dte(self) -> float:
        return max(0, (self.expiry - datetime.utcnow()).total_seconds() / 86400)

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def notional_delta(self) -> float:
        return self.delta * self.quantity * 100  # 100 shares per contract

    @property
    def notional_gamma(self) -> float:
        return self.gamma * self.quantity * 100

    @property
    def notional_vega(self) -> float:
        return self.vega * self.quantity * 100

    @property
    def notional_theta(self) -> float:
        return self.theta * self.quantity * 100

    @property
    def pnl(self) -> float:
        return (self.current_price - self.entry_price) * self.quantity * 100


@dataclass
class MultiLegPosition:
    """Multi-leg options position (spread, straddle, etc.)."""
    position_id: str
    underlying: str
    legs: List[OptionLeg] = field(default_factory=list)
    strategy_name: str = ""  # "straddle", "iron_condor", "vertical_spread", etc.
    opened_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def net_delta(self) -> float:
        return sum(leg.notional_delta for leg in self.legs)

    @property
    def net_gamma(self) -> float:
        return sum(leg.notional_gamma for leg in self.legs)

    @property
    def net_vega(self) -> float:
        return sum(leg.notional_vega for leg in self.legs)

    @property
    def net_theta(self) -> float:
        return sum(leg.notional_theta for leg in self.legs)

    @property
    def total_pnl(self) -> float:
        return sum(leg.pnl for leg in self.legs)

    @property
    def min_dte(self) -> float:
        return min((leg.dte for leg in self.legs), default=0)

    @property
    def num_contracts(self) -> int:
        return sum(abs(leg.quantity) for leg in self.legs)


class OptionsStrategyBase(StrategyBase):
    """Base class for all options strategies.

    Provides Greeks management, volatility surface access, multi-leg tracking,
    and delta-hedging infrastructure. Subclasses focus on signal logic.
    """

    def __init__(self, config: StrategyConfig) -> None:
        config.asset_class = AssetClass.OPTIONS
        super().__init__(config)

        # ── Options positions ──
        self._option_positions: Dict[str, MultiLegPosition] = {}  # pos_id -> position
        self._hedge_shares: Dict[str, float] = {}  # underlying -> hedge share qty

        # ── Portfolio Greeks ──
        self._portfolio_greeks = GreeksExposure()

        # ── Vol data ──
        self._iv_cache: Dict[str, float] = {}  # symbol -> ATM IV
        self._rv_cache: Dict[str, Deque[float]] = {}  # symbol -> recent closes
        self._vol_surface: Dict[str, Any] = {}  # underlying -> surface data

        # ── Risk limits (configurable) ──
        self.max_portfolio_delta = self.get_param("max_portfolio_delta", 500)
        self.max_portfolio_gamma = self.get_param("max_portfolio_gamma", 200)
        self.max_portfolio_vega = self.get_param("max_portfolio_vega", 5000)
        self.max_portfolio_theta = self.get_param("max_portfolio_theta", -1000)
        self.delta_hedge_threshold = self.get_param("delta_hedge_threshold", 50)
        self.max_dte = self.get_param("max_dte", 45)
        self.min_dte = self.get_param("min_dte", 5)
        self.auto_roll_dte = self.get_param("auto_roll_dte", 3)

    # ------------------------------------------------------------------
    # Greeks management
    # ------------------------------------------------------------------

    def update_portfolio_greeks(self) -> GreeksExposure:
        """Recalculate aggregate portfolio Greeks from all positions."""
        delta = gamma = vega = theta = rho = 0.0
        for pos in self._option_positions.values():
            delta += pos.net_delta
            gamma += pos.net_gamma
            vega += pos.net_vega
            theta += pos.net_theta
        # Add hedge shares to delta
        for sym, shares in self._hedge_shares.items():
            delta += shares
        self._portfolio_greeks = GreeksExposure(
            delta=delta, gamma=gamma, vega=vega, theta=theta, rho=rho,
        )
        return self._portfolio_greeks

    @property
    def portfolio_greeks(self) -> GreeksExposure:
        return self._portfolio_greeks

    def needs_delta_hedge(self) -> bool:
        """Check if portfolio delta exceeds hedge threshold."""
        return abs(self._portfolio_greeks.delta) > self.delta_hedge_threshold

    def compute_hedge_qty(self, underlying: str) -> float:
        """Compute shares needed to delta-neutralize the portfolio."""
        return -self._portfolio_greeks.delta

    # ------------------------------------------------------------------
    # Greeks risk checks
    # ------------------------------------------------------------------

    def check_greeks_limits(self) -> List[str]:
        """Return list of violated Greeks limits."""
        violations = []
        g = self._portfolio_greeks
        if abs(g.delta) > self.max_portfolio_delta:
            violations.append(f"delta={g.delta:.0f} > limit={self.max_portfolio_delta}")
        if abs(g.gamma) > self.max_portfolio_gamma:
            violations.append(f"gamma={g.gamma:.1f} > limit={self.max_portfolio_gamma}")
        if abs(g.vega) > self.max_portfolio_vega:
            violations.append(f"vega={g.vega:.0f} > limit={self.max_portfolio_vega}")
        if g.theta < self.max_portfolio_theta:
            violations.append(f"theta={g.theta:.0f} < limit={self.max_portfolio_theta}")
        return violations

    # ------------------------------------------------------------------
    # Volatility data
    # ------------------------------------------------------------------

    def update_iv(self, underlying: str, iv: float) -> None:
        """Update cached ATM implied volatility for an underlying."""
        self._iv_cache[underlying] = iv

    def get_atm_iv(self, underlying: str) -> float:
        """Get cached ATM IV (annualized)."""
        return self._iv_cache.get(underlying, 0.0)

    def realized_vol(self, underlying: str, window: int = 20) -> float:
        """Compute annualized realized vol from cached close prices."""
        closes = self._rv_cache.get(underlying)
        if not closes or len(closes) < window + 1:
            return 0.0
        arr = np.array(list(closes))[-window - 1:]
        returns = np.diff(np.log(arr))
        return float(np.std(returns) * np.sqrt(252))

    def vrp(self, underlying: str, rv_window: int = 20) -> float:
        """Volatility Risk Premium = IV - RV."""
        return self.get_atm_iv(underlying) - self.realized_vol(underlying, rv_window)

    def on_bar(self, symbol: str, bar: Bar) -> None:
        """Cache close prices for RV computation."""
        super().on_bar(symbol, bar)
        if symbol not in self._rv_cache:
            self._rv_cache[symbol] = deque(maxlen=300)
        self._rv_cache[symbol].append(bar.close)

    # ------------------------------------------------------------------
    # Multi-leg position helpers
    # ------------------------------------------------------------------

    def open_position(self, position: MultiLegPosition) -> None:
        """Track a new multi-leg position."""
        self._option_positions[position.position_id] = position

    def close_position(self, position_id: str) -> Optional[MultiLegPosition]:
        """Remove and return a multi-leg position."""
        return self._option_positions.pop(position_id, None)

    def get_positions_by_underlying(self, underlying: str) -> List[MultiLegPosition]:
        """Get all option positions for an underlying."""
        return [p for p in self._option_positions.values() if p.underlying == underlying]

    def positions_needing_roll(self) -> List[MultiLegPosition]:
        """Find positions approaching expiry that need rolling."""
        return [p for p in self._option_positions.values() if p.min_dte <= self.auto_roll_dte]

    @property
    def total_open_positions(self) -> int:
        return len(self._option_positions)

    # ------------------------------------------------------------------
    # Signal helpers for common structures
    # ------------------------------------------------------------------

    def _make_signal(self, symbol: str, direction: SignalDirection, strength: float,
                     target_qty: Optional[float] = None, **kwargs) -> Signal:
        return Signal(
            signal_id=uuid4(),
            strategy_id=self.strategy_id,
            symbol=symbol,
            direction=direction,
            strength=strength,
            timestamp=datetime.utcnow(),
            target_quantity=target_qty,
            metadata=kwargs,
        )
