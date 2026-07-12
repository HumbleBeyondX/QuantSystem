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

    @staticmethod
    def _is_derivative(asset_class: Any, symbol: str) -> bool:
        """Margin instrument? (perp swap / futures — notional never moves cash).

        CCXT swap symbols contain ':' (e.g. 'ETH/USDT:USDT'); asset_class is
        the authoritative flag but restart-synced positions sometimes carry
        plain CRYPTO, so the symbol convention is checked as well.
        """
        name = getattr(asset_class, "value", str(asset_class) if asset_class else "")
        return "perp" in str(name).lower() or ":" in (symbol or "")

    def on_fill(self, fill: Fill) -> None:
        """Update positions based on a fill event.

        Cash accounting is asset-class aware (2026-06 audit P1-6):
          * SPOT: full notional moves cash (buy = pay, sell = receive).
          * DERIVATIVES (perp swaps): notional NEVER moves cash — only
            commissions and realized PnL do. The old spot-style treatment
            credited a short's full notional as cash, inflating a $87 account
            to a fake $468 equity and triggering the 2026-06-07 false
            "75.5% drawdown" circuit breaker on a profitable trade.
        """
        strategy_id = fill.strategy_id
        symbol = fill.symbol
        is_deriv = self._is_derivative(fill.asset_class, symbol)

        if strategy_id not in self._positions:
            self._positions[strategy_id] = {}

        positions = self._positions[strategy_id]

        realized_delta = 0.0
        uncovered_commission = fill.commission  # commission not netted into pnl

        if symbol in positions:
            pos = positions[symbol]
            realized_before = pos.realized_pnl
            uncovered_commission = self._update_existing_position(pos, fill)
            realized_delta = pos.realized_pnl - realized_before
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
        if is_deriv:
            # Margin instrument: realized PnL (commission on covering legs
            # already netted inside) + remaining commission. No notional flow.
            self._cash += realized_delta - uncovered_commission
        else:
            trade_value = fill.price * fill.quantity
            if fill.side == OrderSide.BUY:
                self._cash -= trade_value + fill.commission
            else:
                self._cash += trade_value - fill.commission

    def _update_existing_position(self, pos: Position, fill: Fill) -> float:
        """Update an existing position with a new fill.

        Realized PnL is computed on the COVERING quantity only, with
        commission pro-rated (audit P1-12: the old code used the whole fill
        quantity, overstating PnL on position flips by the flip portion).

        Returns:
            Commission NOT netted into realized PnL (the portion attributable
            to opening/adding rather than covering) — the caller settles it
            against cash.
        """
        same_direction = (
            (fill.side == OrderSide.BUY and pos.side == PositionSide.LONG)
            or (fill.side == OrderSide.SELL and pos.side == PositionSide.SHORT)
        )

        if same_direction:
            # Adding to the existing position — no realized PnL.
            total_cost = pos.average_entry_price * pos.quantity + fill.price * fill.quantity
            pos.quantity += fill.quantity
            pos.average_entry_price = total_cost / pos.quantity if pos.quantity > 0 else 0
            return fill.commission

        # Opposite direction: covering (and possibly flipping).
        covering_qty = min(fill.quantity, pos.quantity)
        flip_qty = fill.quantity - covering_qty
        covering_frac = covering_qty / fill.quantity if fill.quantity > 0 else 0.0
        covering_commission = fill.commission * covering_frac

        if pos.side == PositionSide.SHORT:
            price_pnl = (pos.average_entry_price - fill.price) * covering_qty
        else:
            price_pnl = (fill.price - pos.average_entry_price) * covering_qty
        pos.realized_pnl += price_pnl - covering_commission
        pos.quantity -= covering_qty

        if flip_qty > 1e-12:
            # Flip: remainder opens a fresh position in the fill's direction.
            pos.side = PositionSide.LONG if fill.side == OrderSide.BUY else PositionSide.SHORT
            pos.quantity = flip_qty
            pos.average_entry_price = fill.price
            pos.current_price = fill.price

        return fill.commission - covering_commission

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
        """Get current portfolio snapshot.

        Equity contribution per position (audit fix — the old formula
        `cash + invested + unrealized` double-counted spot appreciation AND
        added derivative notional as if it were owned value):
          * derivative (perp):   unrealized PnL only (margin stays in cash)
          * spot long:           market value (already embeds appreciation)
          * spot short:          -market value (a liability to buy back)
        """
        all_positions = []
        total_unrealized = 0.0
        equity_from_positions = 0.0
        for strategy_positions in self._positions.values():
            for pos in strategy_positions.values():
                all_positions.append(pos)
                total_unrealized += pos.unrealized_pnl
                if self._is_derivative(pos.asset_class, pos.symbol):
                    equity_from_positions += pos.unrealized_pnl
                elif pos.side == PositionSide.LONG:
                    equity_from_positions += pos.market_value
                else:
                    equity_from_positions -= pos.market_value

        total_value = self._cash + equity_from_positions

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
