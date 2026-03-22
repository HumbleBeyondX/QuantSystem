"""Order Management System: manages the full lifecycle of orders."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from core.enums import AssetClass, OrderSide, OrderStatus, OrderType
from core.logging import get_logger
from core.models import Order, TargetPosition

logger = get_logger(__name__)


class OrderManager:
    """Manages order lifecycle: creation, submission, tracking, and completion."""

    def __init__(self) -> None:
        self._orders: Dict[UUID, Order] = {}
        self._broker_map: Dict[str, UUID] = {}  # broker_id -> order_id

    def create_order(
        self,
        strategy_id: str,
        symbol: str,
        side: OrderSide,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
        asset_class: AssetClass = AssetClass.CRYPTO,
        metadata: Optional[Dict] = None,
    ) -> Order:
        """Create a new order."""
        order = Order(
            order_id=uuid4(),
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
            stop_price=stop_price,
            asset_class=asset_class,
            metadata=metadata or {},
        )
        self._orders[order.order_id] = order
        logger.info("order_created", order_id=str(order.order_id), symbol=symbol, side=side.value)
        return order

    def create_orders_from_targets(
        self,
        targets: List[TargetPosition],
        current_positions: Dict[str, float],
        prices: Dict[str, float],
    ) -> List[Order]:
        """Convert target positions to orders by computing the delta."""
        orders: List[Order] = []

        for target in targets:
            current = current_positions.get(target.symbol, 0.0)
            delta = target.target_quantity - current

            if abs(delta) < 1e-10:
                continue

            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            order = self.create_order(
                strategy_id=target.strategy_id,
                symbol=target.symbol,
                side=side,
                quantity=abs(delta),
                order_type=OrderType.MARKET,
            )
            orders.append(order)

        return orders

    def update_status(
        self,
        order_id: UUID,
        status: OrderStatus,
        broker_order_id: Optional[str] = None,
    ) -> None:
        """Update order status."""
        order = self._orders.get(order_id)
        if not order:
            logger.warning("order_not_found", order_id=str(order_id))
            return

        order.status = status
        order.updated_at = datetime.utcnow()
        if broker_order_id:
            order.broker_order_id = broker_order_id
            self._broker_map[broker_order_id] = order_id

    def get_order(self, order_id: UUID) -> Optional[Order]:
        return self._orders.get(order_id)

    def get_order_by_broker_id(self, broker_id: str) -> Optional[Order]:
        order_id = self._broker_map.get(broker_id)
        return self._orders.get(order_id) if order_id else None

    def get_pending_orders(self) -> List[Order]:
        return [o for o in self._orders.values() if o.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED)]

    def get_orders_by_strategy(self, strategy_id: str) -> List[Order]:
        return [o for o in self._orders.values() if o.strategy_id == strategy_id]

    def cancel_all_pending(self) -> List[UUID]:
        """Cancel all pending orders. Returns list of cancelled order IDs."""
        cancelled = []
        for order in self.get_pending_orders():
            order.status = OrderStatus.CANCELLED
            order.updated_at = datetime.utcnow()
            cancelled.append(order.order_id)
        return cancelled
