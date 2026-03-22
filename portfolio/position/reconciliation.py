"""Position reconciliation: compare internal positions vs broker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ReconciliationResult:
    symbol: str
    internal_qty: float
    broker_qty: float
    difference: float
    is_matched: bool


class PositionReconciler:
    """Reconciles internal position tracking with broker/exchange positions."""

    def __init__(self, tolerance: float = 0.001) -> None:
        self.tolerance = tolerance

    def reconcile(
        self,
        internal: Dict[str, float],  # symbol -> qty
        broker: Dict[str, float],    # symbol -> qty
    ) -> List[ReconciliationResult]:
        """Compare internal vs broker positions."""
        results: List[ReconciliationResult] = []
        all_symbols = set(internal.keys()) | set(broker.keys())

        for symbol in all_symbols:
            int_qty = internal.get(symbol, 0.0)
            brk_qty = broker.get(symbol, 0.0)
            diff = int_qty - brk_qty
            is_matched = abs(diff) < self.tolerance

            results.append(ReconciliationResult(
                symbol=symbol,
                internal_qty=int_qty,
                broker_qty=brk_qty,
                difference=diff,
                is_matched=is_matched,
            ))

            if not is_matched:
                logger.warning(
                    "position_mismatch",
                    symbol=symbol,
                    internal=int_qty,
                    broker=brk_qty,
                    diff=diff,
                )

        return results
