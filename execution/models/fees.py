"""Fee models for different brokers and exchanges."""

from __future__ import annotations

from abc import ABC, abstractmethod


class FeeModel(ABC):
    @abstractmethod
    def compute(self, price: float, quantity: float) -> float:
        ...


class PercentageFee(FeeModel):
    """Simple percentage-based fee."""
    def __init__(self, rate: float = 0.001) -> None:
        self.rate = rate

    def compute(self, price: float, quantity: float) -> float:
        return price * quantity * self.rate


class IBFeeModel(FeeModel):
    """Interactive Brokers tiered fee model for US equities."""
    def compute(self, price: float, quantity: float) -> float:
        # IB tiered: $0.0035/share, min $0.35, max 1% of trade value
        fee = max(quantity * 0.0035, 0.35)
        return min(fee, price * quantity * 0.01)


class CryptoMakerTakerFee(FeeModel):
    """Crypto exchange maker/taker fee model."""
    def __init__(self, maker: float = 0.0002, taker: float = 0.0005, is_maker: bool = False) -> None:
        self.maker = maker
        self.taker = taker
        self.is_maker = is_maker

    def compute(self, price: float, quantity: float) -> float:
        rate = self.maker if self.is_maker else self.taker
        return price * quantity * rate


class ZeroFee(FeeModel):
    """No fees (for ideal backtests)."""
    def compute(self, price: float, quantity: float) -> float:
        return 0.0
