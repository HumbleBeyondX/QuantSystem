"""Order router: routes orders to the correct execution engine based on asset class."""

from __future__ import annotations

from typing import Dict

from core.enums import AssetClass
from core.logging import get_logger
from core.models import Order
from execution.engine.base import ExecutionEngine

logger = get_logger(__name__)


class OrderRouter:
    """Routes orders to appropriate execution engines."""

    def __init__(self) -> None:
        self._engines: Dict[AssetClass, ExecutionEngine] = {}

    def register_engine(self, asset_class: AssetClass, engine: ExecutionEngine) -> None:
        self._engines[asset_class] = engine
        logger.info("engine_registered", asset_class=asset_class.value, engine=type(engine).__name__)

    async def route(self, order: Order) -> str:
        """Route an order to the appropriate engine. Returns broker order ID."""
        engine = self._engines.get(order.asset_class)
        if not engine:
            raise ValueError(f"No engine registered for {order.asset_class.value}")

        return await engine.submit_order(order)

    async def cancel(self, order: Order) -> bool:
        """Cancel an order through the appropriate engine."""
        engine = self._engines.get(order.asset_class)
        if not engine or not order.broker_order_id:
            return False
        return await engine.cancel_order(order.broker_order_id)
