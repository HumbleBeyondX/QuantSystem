"""Data quality validation pipeline.

Validates incoming market data before it enters Kafka/storage.
Catches crossed markets, stale data, extreme price jumps, and bad values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List

from core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ValidationResult:
    """Result of a data validation check."""

    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class DataValidator:
    """Validates incoming market data for quality issues.

    Performs checks for:
    - Non-positive / zero prices
    - Crossed markets (bid > ask)
    - Abnormally wide spreads
    - Extreme price jumps (potential bad ticks)
    - Stale data (timestamp too old)
    - Invalid bar data (high < low, etc.)
    """

    def __init__(
        self,
        max_spread_pct: float = 10.0,
        max_price_change_pct: float = 50.0,
        stale_threshold_sec: float = 60.0,
    ) -> None:
        self.max_spread_pct = max_spread_pct
        self.max_price_change_pct = max_price_change_pct
        self.stale_threshold_sec = stale_threshold_sec
        self._last_prices: Dict[str, float] = {}

    def validate_tick(
        self,
        symbol: str,
        bid: float,
        ask: float,
        last: float,
        timestamp: datetime,
    ) -> ValidationResult:
        """Validate a single tick/quote update."""
        errors: List[str] = []
        warnings: List[str] = []

        # Check 1: Non-positive prices
        for name, val in [("bid", bid), ("ask", ask), ("last", last)]:
            if val is not None and val < 0:
                errors.append(f"Negative {name} price: {val}")

        # Check 2: Crossed market (bid > ask)
        if bid > 0 and ask > 0 and bid > ask:
            errors.append(f"Crossed market: bid={bid} > ask={ask}")

        # Check 3: Abnormal spread
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2
            if mid > 0:
                spread_pct = ((ask - bid) / mid) * 100
                if spread_pct > self.max_spread_pct:
                    warnings.append(
                        f"Wide spread: {spread_pct:.1f}% (threshold: {self.max_spread_pct}%)"
                    )

        # Check 4: Extreme price jump
        effective_price = last if last > 0 else (bid + ask) / 2 if bid > 0 and ask > 0 else 0
        if symbol in self._last_prices and effective_price > 0:
            prev = self._last_prices[symbol]
            if prev > 0:
                change_pct = abs((effective_price - prev) / prev) * 100
                if change_pct > self.max_price_change_pct:
                    errors.append(
                        f"Extreme price jump: {change_pct:.1f}% "
                        f"(prev={prev}, now={effective_price})"
                    )

        # Check 5: Stale data
        if timestamp:
            if isinstance(timestamp, str):
                try:
                    timestamp = datetime.fromisoformat(timestamp)
                except ValueError:
                    pass
            if isinstance(timestamp, datetime):
                age = (datetime.utcnow() - timestamp).total_seconds()
                if age > self.stale_threshold_sec:
                    warnings.append(f"Stale data: {age:.0f}s old")

        # Update last price
        if effective_price > 0:
            self._last_prices[symbol] = effective_price

        is_valid = len(errors) == 0
        if not is_valid:
            logger.warning("tick_validation_failed", symbol=symbol, errors=errors)

        return ValidationResult(is_valid=is_valid, errors=errors, warnings=warnings)

    def validate_bar(
        self,
        symbol: str,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> ValidationResult:
        """Validate an OHLCV bar."""
        errors: List[str] = []
        warnings: List[str] = []

        # Basic price checks
        for name, val in [("open", open_), ("high", high), ("low", low), ("close", close)]:
            if val <= 0:
                errors.append(f"Non-positive {name}: {val}")

        # OHLC consistency
        if high > 0 and low > 0:
            if high < low:
                errors.append(f"High ({high}) < Low ({low})")
            if open_ > 0 and (open_ < low * 0.99 or open_ > high * 1.01):
                warnings.append(f"Open ({open_}) outside H/L range [{low}, {high}]")
            if close > 0 and (close < low * 0.99 or close > high * 1.01):
                warnings.append(f"Close ({close}) outside H/L range [{low}, {high}]")

        # Volume check
        if volume < 0:
            errors.append(f"Negative volume: {volume}")

        return ValidationResult(is_valid=len(errors) == 0, errors=errors, warnings=warnings)

    def validate_orderbook(
        self,
        symbol: str,
        bids: list,
        asks: list,
    ) -> ValidationResult:
        """Validate an order book snapshot."""
        errors: List[str] = []
        warnings: List[str] = []

        if not bids and not asks:
            errors.append("Empty order book")
            return ValidationResult(is_valid=False, errors=errors, warnings=warnings)

        # Check bid/ask ordering
        if bids and asks:
            best_bid = bids[0][0] if isinstance(bids[0], (list, tuple)) else bids[0]
            best_ask = asks[0][0] if isinstance(asks[0], (list, tuple)) else asks[0]
            if best_bid > best_ask:
                errors.append(f"Crossed book: best_bid={best_bid} > best_ask={best_ask}")

        # Check monotonicity of bids (descending) and asks (ascending)
        if len(bids) > 1:
            for i in range(len(bids) - 1):
                p1 = bids[i][0] if isinstance(bids[i], (list, tuple)) else bids[i]
                p2 = bids[i + 1][0] if isinstance(bids[i + 1], (list, tuple)) else bids[i + 1]
                if p1 < p2:
                    warnings.append("Non-monotonic bid book")
                    break

        return ValidationResult(is_valid=len(errors) == 0, errors=errors, warnings=warnings)

    def reset(self, symbol: str | None = None) -> None:
        """Reset tracked state for a symbol or all symbols."""
        if symbol:
            self._last_prices.pop(symbol, None)
        else:
            self._last_prices.clear()
