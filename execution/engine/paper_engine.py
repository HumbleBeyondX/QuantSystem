"""Paper trading engine: simulates order execution with realistic fills.

Supports market, limit, and stop orders with configurable slippage and fee models.
Used for strategy testing before going live.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from core.enums import OrderSide, OrderStatus, OrderType
from core.logging import get_logger
from core.models import Fill, Order
from execution.engine.base import ExecutionEngine
from execution.models.fees import FeeModel, PercentageFee
from execution.models.slippage import SlippageModel, FixedSlippage

logger = get_logger(__name__)


class PaperEngine(ExecutionEngine):
    """Simulated execution engine for paper trading."""

    def __init__(
        self,
        slippage_model: Optional[SlippageModel] = None,
        fee_model: Optional[FeeModel] = None,
        fill_delay_ms: int = 50,
    ) -> None:
        self._slippage = slippage_model or FixedSlippage(bps=5)
        self._fee_model = fee_model or PercentageFee(rate=0.001)
        self._fill_delay_ms = fill_delay_ms

        self._prices: Dict[str, float] = {}  # symbol -> last price
        self._pending_orders: Dict[str, Order] = {}
        self._fills: List[Fill] = []
        self._positions: Dict[str, float] = {}
        self._connected = False

    async def connect(self) -> None:
        self._connected = True
        logger.info("paper_engine_connected")

    async def disconnect(self) -> None:
        self._connected = False
        logger.info("paper_engine_disconnected")

    def update_price(self, symbol: str, price: float) -> None:
        """Update market price for fill simulation."""
        self._prices[symbol] = price
        # Check pending limit/stop orders
        self._check_pending_orders(symbol, price)

    async def submit_order(self, order: Order) -> str:
        """Simulate order submission."""
        broker_id = f"PAPER-{uuid4().hex[:8]}"
        order.broker_order_id = broker_id
        order.status = OrderStatus.SUBMITTED
        order.updated_at = datetime.utcnow()

        if order.order_type == OrderType.MARKET:
            # Fill immediately
            await asyncio.sleep(self._fill_delay_ms / 1000)
            self._execute_fill(order)
        else:
            # Queue for later
            self._pending_orders[broker_id] = order

        logger.info(
            "paper_order_submitted",
            order_id=str(order.order_id),
            symbol=order.symbol,
            side=order.side.value,
            qty=order.quantity,
            type=order.order_type.value,
        )
        return broker_id

    async def cancel_order(self, order_id: str) -> bool:
        if order_id in self._pending_orders:
            order = self._pending_orders.pop(order_id)
            order.status = OrderStatus.CANCELLED
            return True
        return False

    async def get_fills(self) -> List[Fill]:
        fills = self._fills.copy()
        self._fills.clear()
        return fills

    async def get_positions(self) -> Dict[str, float]:
        return self._positions.copy()

    async def health_check(self) -> bool:
        return self._connected

    def _execute_fill(self, order: Order) -> None:
        """Simulate a fill with slippage and fees."""
        base_price = self._prices.get(order.symbol, order.limit_price or 0)
        if base_price == 0:
            logger.warning("paper_no_price", symbol=order.symbol)
            order.status = OrderStatus.REJECTED
            return

        # Apply slippage
        fill_price = self._slippage.apply(base_price, order.side, order.quantity)

        # Compute commission
        commission = self._fee_model.compute(fill_price, order.quantity)

        # Create fill
        fill = Fill(
            fill_id=uuid4(),
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            price=fill_price,
            quantity=order.quantity,
            commission=commission,
            timestamp=datetime.utcnow(),
            strategy_id=order.strategy_id,
            asset_class=order.asset_class,
        )
        self._fills.append(fill)

        # Update order
        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.average_fill_price = fill_price
        order.commission = commission
        order.updated_at = datetime.utcnow()

        # Update internal position
        current = self._positions.get(order.symbol, 0)
        if order.side == OrderSide.BUY:
            self._positions[order.symbol] = current + order.quantity
        else:
            self._positions[order.symbol] = current - order.quantity

        logger.info(
            "paper_fill",
            symbol=order.symbol,
            side=order.side.value,
            price=fill_price,
            qty=order.quantity,
            commission=commission,
        )

    def _check_pending_orders(self, symbol: str, price: float) -> None:
        """Check if any pending limit/stop orders should be filled."""
        to_remove = []
        for broker_id, order in self._pending_orders.items():
            if order.symbol != symbol:
                continue

            should_fill = False
            if order.order_type == OrderType.LIMIT:
                if order.side == OrderSide.BUY and price <= order.limit_price:
                    should_fill = True
                elif order.side == OrderSide.SELL and price >= order.limit_price:
                    should_fill = True

            elif order.order_type == OrderType.STOP:
                if order.side == OrderSide.BUY and price >= order.stop_price:
                    should_fill = True
                elif order.side == OrderSide.SELL and price <= order.stop_price:
                    should_fill = True

            if should_fill:
                self._execute_fill(order)
                to_remove.append(broker_id)

        for broker_id in to_remove:
            self._pending_orders.pop(broker_id, None)
