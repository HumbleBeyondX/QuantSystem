"""Execution engine abstract base class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from core.models import Fill, Order


class ExecutionEngine(ABC):
    """Abstract base for all execution engines (paper, IB, CCXT)."""

    @abstractmethod
    async def connect(self) -> None:
        """Connect to the broker/exchange."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the broker/exchange."""
        ...

    @abstractmethod
    async def submit_order(self, order: Order) -> str:
        """Submit an order. Returns broker order ID."""
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order. Returns success."""
        ...

    @abstractmethod
    async def get_fills(self) -> List[Fill]:
        """Get recent fills."""
        ...

    @abstractmethod
    async def get_positions(self) -> dict:
        """Get current positions from broker."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Check broker connectivity."""
        ...
