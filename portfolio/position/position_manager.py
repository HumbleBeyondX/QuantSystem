"""Position manager: track all positions across strategies."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from core.enums import OrderSide, PositionSide
from core.logging import get_logger
from core.models import Fill, Position, PortfolioSnapshot

logger = get_logger(__name__)


class PositionManager:
    """Tracks all open positions across all strategies."""

    def __init__(self, initial_cash: float = 100_000) -> None:
        self._positions: Dict[str, Dict[str, Position]] = {}  # strategy_id -> {symbol -> Position}
        self._cash = initial_cash
        self._initial_cash = initial_cash
        self._total_realized_pnl = 0.0

    def on_fill(self, fill: Fill) -> None:
        """Update positions based on a fill event."""
        strategy_id = fill.strategy_id
        symbol = fill.symbol

        if strategy_id not in self._positions:
            self._positions[strategy_id] = {}

        positions = self._positions[strategy_id]

        if symbol in positions:
            pos = positions[symbol]
            self._update_existing_position(pos, fill)
            if abs(pos.quantity) < 1e-10:
                # Position closed
                self._total_realized_pnl += pos.realized_pnl
                del positions[symbol]
        else:
            # New position
            side = PositionSide.LONG if fill.side == OrderSide.BUY else PositionSide.SHORT
            positions[symbol] = Position(
                symbol=symbol,
                strategy_id=strategy_id,
                side=side,
                quantity=fill.quantity,
                average_entry_price=fill.price,
                current_price=fill.price,
                asset_class=fill.asset_class,
            )

        # Update cash
        trade_value = fill.price * fill.quantity
        if fill.side == OrderSide.BUY:
            self._cash -= trade_value + fill.commission
        else:
            self._cash += trade_value - fill.commission

    def _update_existing_position(self, pos: Position, fill: Fill) -> None:
        """Update an existing position with a new fill."""
        if fill.side == OrderSide.BUY:
            if pos.side == PositionSide.LONG:
                # Adding to long
                total_cost = pos.average_entry_price * pos.quantity + fill.price * fill.quantity
                pos.quantity += fill.quantity
                pos.average_entry_price = total_cost / pos.quantity if pos.quantity > 0 else 0
            else:
                # Covering short
                pnl = (pos.average_entry_price - fill.price) * fill.quantity - fill.commission
                pos.realized_pnl += pnl
                pos.quantity -= fill.quantity
                if pos.quantity < 0:
                    # Flipped to long
                    pos.side = PositionSide.LONG
                    pos.quantity = abs(pos.quantity)
                    pos.average_entry_price = fill.price
        else:  # SELL
            if pos.side == PositionSide.SHORT:
                # Adding to short
                total_cost = pos.average_entry_price * pos.quantity + fill.price * fill.quantity
                pos.quantity += fill.quantity
                pos.average_entry_price = total_cost / pos.quantity if pos.quantity > 0 else 0
            else:
                # Closing long
                pnl = (fill.price - pos.average_entry_price) * fill.quantity - fill.commission
                pos.realized_pnl += pnl
                pos.quantity -= fill.quantity
                if pos.quantity < 0:
                    pos.side = PositionSide.SHORT
                    pos.quantity = abs(pos.quantity)
                    pos.average_entry_price = fill.price

    def update_prices(self, prices: Dict[str, float]) -> None:
        """Update current market prices for unrealized PnL."""
        for strategy_positions in self._positions.values():
            for symbol, pos in strategy_positions.items():
                if symbol in prices:
                    pos.current_price = prices[symbol]
                    if pos.side == PositionSide.LONG:
                        pos.unrealized_pnl = (pos.current_price - pos.average_entry_price) * pos.quantity
                    else:
                        pos.unrealized_pnl = (pos.average_entry_price - pos.current_price) * pos.quantity

    def get_snapshot(self) -> PortfolioSnapshot:
        """Get current portfolio snapshot."""
        all_positions = []
        total_unrealized = 0.0
        for strategy_positions in self._positions.values():
            for pos in strategy_positions.values():
                all_positions.append(pos)
                total_unrealized += pos.unrealized_pnl

        invested = sum(p.market_value for p in all_positions)
        total_value = self._cash + invested + total_unrealized

        return PortfolioSnapshot(
            timestamp=datetime.utcnow(),
            total_value=total_value,
            cash=self._cash,
            positions=all_positions,
            unrealized_pnl=total_unrealized,
            realized_pnl=self._total_realized_pnl,
            daily_pnl=total_value - self._initial_cash,
        )

    def get_positions_by_strategy(self, strategy_id: str) -> List[Position]:
        return list(self._positions.get(strategy_id, {}).values())

    def get_all_positions(self) -> List[Position]:
        return [
            pos
            for positions in self._positions.values()
            for pos in positions.values()
        ]

    @property
    def cash(self) -> float:
        return self._cash
