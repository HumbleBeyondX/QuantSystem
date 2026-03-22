"""Delta-neutral options hedging strategy.

Maintains a delta-neutral portfolio by continuously monitoring and rebalancing
the aggregate Greeks exposure. Sells premium (short straddles/strangles) when
IV is high, hedges with underlying stock/futures.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional
from uuid import uuid4

from core.enums import SignalDirection
from core.models import GreeksExposure, OptionContract, Signal, TargetPosition
from strategies.base import StrategyBase, StrategyConfig
from strategies.registry import register_strategy


@register_strategy("options_delta_neutral")
class DeltaNeutralStrategy(StrategyBase):
    """Delta-neutral options portfolio management."""

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

        # Parameters
        self.delta_tolerance = self.get_param("delta_tolerance", 0.05)
        self.rebalance_interval_bars = self.get_param("rebalance_interval_minutes", 15)
        self.max_vega = self.get_param("max_vega_exposure", 5000)
        self.max_gamma = self.get_param("max_gamma_exposure", 200)
        self.max_theta = self.get_param("max_theta_daily", -500)
        self.preferred_delta = self.get_param("preferred_delta", 0.30)
        self.min_dte = self.get_param("min_dte", 7)
        self.max_dte = self.get_param("max_dte", 45)
        self.min_iv_percentile = self.get_param("min_iv_percentile", 30)
        self.hedge_instrument = self.get_param("hedge_instrument", "stock")

        # State
        self._options_positions: Dict[str, List[OptionContract]] = {}
        self._portfolio_greeks: GreeksExposure = GreeksExposure()
        self._iv_percentile: Dict[str, float] = {}
        self._bars_since_rebalance = 0

    def update_options_positions(
        self, underlying: str, contracts: List[OptionContract]
    ) -> None:
        """Update current options positions for an underlying."""
        self._options_positions[underlying] = contracts
        self._compute_portfolio_greeks()

    def update_iv_percentile(self, underlying: str, percentile: float) -> None:
        """Update IV percentile rank for an underlying."""
        self._iv_percentile[underlying] = percentile

    def _compute_portfolio_greeks(self) -> None:
        """Aggregate Greeks across all options positions."""
        total = GreeksExposure()
        for underlying, contracts in self._options_positions.items():
            for opt in contracts:
                total.delta += opt.delta * opt.multiplier
                total.gamma += opt.gamma * opt.multiplier
                total.vega += opt.vega * opt.multiplier
                total.theta += opt.theta * opt.multiplier
                total.rho += opt.rho * opt.multiplier
        self._portfolio_greeks = total

    def generate_signals(self) -> List[Signal]:
        """Generate delta-hedging and premium selling signals."""
        signals: List[Signal] = []
        now = datetime.utcnow()
        self._bars_since_rebalance += 1

        if self._bars_since_rebalance < self.rebalance_interval_bars:
            return signals

        self._bars_since_rebalance = 0
        greeks = self._portfolio_greeks

        # Signal 1: Delta hedge - rebalance if delta exceeds tolerance
        for underlying in self.config.symbols:
            underlying_delta = sum(
                opt.delta * opt.multiplier
                for opt in self._options_positions.get(underlying, [])
            )

            stock_pos = self.get_position(underlying)
            net_delta = underlying_delta + stock_pos

            if abs(net_delta) > self.delta_tolerance * 100:
                # Need to hedge: buy/sell stock to neutralize delta
                hedge_qty = -net_delta  # Negate to offset

                direction = SignalDirection.LONG if hedge_qty > 0 else SignalDirection.SHORT
                signals.append(Signal(
                    signal_id=uuid4(),
                    strategy_id=self.strategy_id,
                    symbol=underlying,
                    direction=direction,
                    strength=0.9,
                    timestamp=now,
                    target_quantity=abs(hedge_qty),
                    metadata={
                        "reason": "delta_hedge",
                        "net_delta": net_delta,
                        "hedge_qty": hedge_qty,
                        "portfolio_greeks": {
                            "delta": greeks.delta,
                            "gamma": greeks.gamma,
                            "vega": greeks.vega,
                            "theta": greeks.theta,
                        },
                    },
                ))

        # Signal 2: Check risk limits
        if abs(greeks.vega) > self.max_vega:
            signals.append(Signal(
                signal_id=uuid4(),
                strategy_id=self.strategy_id,
                symbol="PORTFOLIO",
                direction=SignalDirection.FLAT,
                strength=1.0,
                timestamp=now,
                metadata={"reason": "vega_limit_breach", "vega": greeks.vega},
            ))

        return signals

    def calculate_target_positions(self) -> List[TargetPosition]:
        """Convert hedge signals to target positions."""
        signals = self.generate_signals()
        self._signals.extend(signals)

        targets: List[TargetPosition] = []
        now = datetime.utcnow()

        for sig in signals:
            if sig.symbol == "PORTFOLIO":
                continue  # Risk alert, not a position target

            targets.append(TargetPosition(
                symbol=sig.symbol,
                strategy_id=self.strategy_id,
                target_quantity=sig.target_quantity or 0,
                urgency=0.8,  # High urgency for delta hedging
                timestamp=now,
            ))

        return targets
