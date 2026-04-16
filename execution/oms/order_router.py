"""Order router: routes orders to the correct execution engine based on asset class.

Supports multiple engines per asset class (e.g., different exchanges for crypto)
and fallback routing for related asset classes (CRYPTO_PERP/CRYPTO_SPOT -> CRYPTO).
"""

from __future__ import annotations

from typing import Dict, Optional

from core.enums import AssetClass
from core.logging import get_logger
from core.models import Order
from execution.engine.base import ExecutionEngine

logger = get_logger(__name__)

# Fallback chains: if no engine for exact asset class, try these
_FALLBACK_CHAINS: Dict[AssetClass, list] = {
    AssetClass.CRYPTO_PERP: [AssetClass.CRYPTO],
    AssetClass.CRYPTO_SPOT: [AssetClass.CRYPTO],
}


class OrderRouter:
    """Routes orders to appropriate execution engines.

    Supports:
    - One engine per asset class (simplest setup)
    - Named engines for multi-exchange routing (e.g., binance, okx)
    - Fallback chains for related asset classes
    """

    def __init__(self) -> None:
        self._engines: Dict[AssetClass, ExecutionEngine] = {}
        self._named_engines: Dict[str, ExecutionEngine] = {}  # name -> engine

    def register_engine(self, asset_class: AssetClass, engine: ExecutionEngine, name: Optional[str] = None) -> None:
        """Register an execution engine for an asset class.

        Args:
            asset_class: The asset class this engine handles.
            engine: The execution engine instance.
            name: Optional name for multi-engine routing (e.g., "binance", "okx").
        """
        self._engines[asset_class] = engine
        if name:
            self._named_engines[name] = engine
        logger.info(
            "engine_registered",
            asset_class=asset_class.value,
            engine=type(engine).__name__,
            name=name or "default",
        )

    def get_engine(self, asset_class: AssetClass, name: Optional[str] = None) -> Optional[ExecutionEngine]:
        """Look up an engine by asset class (with fallback) or by name."""
        if name and name in self._named_engines:
            return self._named_engines[name]

        engine = self._engines.get(asset_class)
        if engine:
            return engine

        # Try fallback chain
        for fallback in _FALLBACK_CHAINS.get(asset_class, []):
            engine = self._engines.get(fallback)
            if engine:
                return engine

        return None

    async def route(self, order: Order) -> str:
        """Route an order to the appropriate engine. Returns broker order ID."""
        # Check if order specifies a target exchange
        target_exchange = order.metadata.get("exchange") if order.metadata else None
        engine = self.get_engine(order.asset_class, name=target_exchange)

        if not engine:
            raise ValueError(
                f"No engine registered for {order.asset_class.value}"
                + (f" (exchange={target_exchange})" if target_exchange else "")
            )

        return await engine.submit_order(order)

    async def cancel(self, order: Order) -> bool:
        """Cancel an order through the appropriate engine."""
        target_exchange = order.metadata.get("exchange") if order.metadata else None
        engine = self.get_engine(order.asset_class, name=target_exchange)
        if not engine or not order.broker_order_id:
            return False
        return await engine.cancel_order(order.broker_order_id)

    @property
    def registered_engines(self) -> Dict[str, str]:
        """Return a summary of registered engines."""
        result = {}
        for ac, eng in self._engines.items():
            result[ac.value] = type(eng).__name__
        for name, eng in self._named_engines.items():
            result[f"named:{name}"] = type(eng).__name__
        return result
