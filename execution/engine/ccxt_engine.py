"""CCXT Pro live execution engine for crypto exchanges.

Placeholder for Phase 5 - connects to Binance/OKX/Bybit via CCXT Pro.
"""

from __future__ import annotations

from typing import Dict, List

from core.logging import get_logger
from core.models import Fill, Order
from execution.engine.base import ExecutionEngine

logger = get_logger(__name__)


class CCXTExecutionEngine(ExecutionEngine):
    """Live execution engine for crypto exchanges via CCXT Pro.

    Supports spot and perpetual futures.
    Full implementation in Phase 5.
    """

    async def connect(self) -> None:
        logger.info("ccxt_engine: connect() - placeholder for Phase 5")
        raise NotImplementedError("CCXT execution engine not yet implemented")

    async def disconnect(self) -> None:
        pass

    async def submit_order(self, order: Order) -> str:
        raise NotImplementedError("CCXT execution engine not yet implemented")

    async def cancel_order(self, order_id: str) -> bool:
        raise NotImplementedError

    async def get_fills(self) -> List[Fill]:
        return []

    async def get_positions(self) -> Dict[str, float]:
        return {}

    async def health_check(self) -> bool:
        return False
