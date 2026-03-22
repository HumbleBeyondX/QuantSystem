"""Slippage models for realistic order fill simulation."""

from __future__ import annotations

from abc import ABC, abstractmethod

from core.enums import OrderSide


class SlippageModel(ABC):
    @abstractmethod
    def apply(self, price: float, side: OrderSide, quantity: float) -> float:
        ...


class FixedSlippage(SlippageModel):
    """Fixed basis point slippage."""
    def __init__(self, bps: float = 5) -> None:
        self.bps = bps

    def apply(self, price: float, side: OrderSide, quantity: float) -> float:
        slip = price * (self.bps / 10_000)
        return price + slip if side == OrderSide.BUY else price - slip


class ProportionalSlippage(SlippageModel):
    """Volume-dependent slippage: larger orders get more slippage."""
    def __init__(self, base_bps: float = 2, impact_factor: float = 0.1) -> None:
        self.base_bps = base_bps
        self.impact_factor = impact_factor

    def apply(self, price: float, side: OrderSide, quantity: float) -> float:
        bps = self.base_bps + self.impact_factor * (quantity ** 0.5)
        slip = price * (bps / 10_000)
        return price + slip if side == OrderSide.BUY else price - slip


class ZeroSlippage(SlippageModel):
    """No slippage (for ideal backtests)."""
    def apply(self, price: float, side: OrderSide, quantity: float) -> float:
        return price
