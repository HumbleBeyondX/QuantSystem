"""Fill tracker: processes fills and updates positions."""

from __future__ import annotations

from typing import Callable, List, Optional

from core.logging import get_logger
from core.models import Fill
from portfolio.position.position_manager import PositionManager

logger = get_logger(__name__)


class FillTracker:
    """Processes fills from execution engines and updates position manager."""

    def __init__(self, position_manager: PositionManager) -> None:
        self._pm = position_manager
        self._callbacks: List[Callable[[Fill], None]] = []
        self._total_fills = 0

    def on_fill_callback(self, callback: Callable[[Fill], None]) -> None:
        """Register a callback to be called on each fill."""
        self._callbacks.append(callback)

    def process_fill(self, fill: Fill) -> None:
        """Process a single fill: update positions and notify callbacks."""
        self._pm.on_fill(fill)
        self._total_fills += 1

        logger.info(
            "fill_processed",
            symbol=fill.symbol,
            side=fill.side.value,
            price=fill.price,
            qty=fill.quantity,
            strategy=fill.strategy_id,
        )

        for callback in self._callbacks:
            try:
                callback(fill)
            except Exception as e:
                logger.error("fill_callback_error", error=str(e))

    def process_fills(self, fills: List[Fill]) -> None:
        for fill in fills:
            self.process_fill(fill)

    @property
    def total_fills(self) -> int:
        return self._total_fills
