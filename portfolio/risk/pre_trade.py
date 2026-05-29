"""Pre-trade risk checks: validate orders before submission."""

from __future__ import annotations

from typing import Any, Dict, Tuple

from core.enums import OrderSide, PositionSide
from core.logging import get_logger
from core.models import Order, PortfolioSnapshot

logger = get_logger(__name__)


class PreTradeRiskCheck:
    """Validates orders against risk limits before they reach the execution engine."""

    def __init__(self, config: Dict[str, Any] = None) -> None:
        config = config or {}
        self.max_order_value = config.get("max_order_value", 50_000)
        self.max_position_pct = config.get("max_position_pct", 0.20)  # 20% of portfolio
        self.max_leverage = config.get("max_leverage", 2.0)
        self.max_daily_orders = config.get("max_daily_orders", 200)
        self.max_daily_loss_pct = config.get("max_daily_loss_pct", 0.05)  # 5%

        self._daily_order_count = 0
        self._daily_pnl = 0.0

    @staticmethod
    def _is_risk_reducing(order: Order, portfolio: PortfolioSnapshot) -> bool:
        """True if the order reduces (or closes) an existing position.

        A SELL against a LONG, or a BUY against a SHORT, lowers exposure and
        must NEVER be blocked by concentration / leverage / daily-loss limits —
        those limits exist to stop INCREASING risk, not to trap a position.
        """
        existing = next(
            (p for p in portfolio.positions if p.symbol == order.symbol), None
        )
        if existing is None or abs(existing.quantity) < 1e-12:
            return False  # opening a new position — not risk-reducing
        if existing.side == PositionSide.LONG and order.side == OrderSide.SELL:
            return True
        if existing.side == PositionSide.SHORT and order.side == OrderSide.BUY:
            return True
        return False

    def check(self, order: Order, portfolio: PortfolioSnapshot) -> Tuple[bool, str]:
        """Run all pre-trade checks.

        Returns: (approved, reason)
        """
        # ── Risk-reducing orders always pass ──
        # Closing / trimming a position lowers exposure; blocking it would
        # trap a losing position past its stop-loss. Only opening / adding
        # orders are subject to the limits below.
        if self._is_risk_reducing(order, portfolio):
            self._daily_order_count += 1
            return True, "approved (risk-reducing)"

        # Check 1: Order size limit
        estimated_value = order.quantity * (order.limit_price or 0)
        if estimated_value > self.max_order_value and order.limit_price:
            return False, f"Order value ${estimated_value:.0f} exceeds max ${self.max_order_value}"

        # Check 2: Position concentration
        if portfolio.total_value > 0:
            existing = next(
                (p for p in portfolio.positions if p.symbol == order.symbol), None
            )
            existing_value = existing.market_value if existing else 0
            new_value = existing_value + estimated_value
            concentration = abs(new_value) / portfolio.total_value
            if concentration > self.max_position_pct:
                return False, (
                    f"Position concentration {concentration:.1%} "
                    f"exceeds max {self.max_position_pct:.1%}"
                )

        # Check 3: Portfolio leverage
        if portfolio.total_value > 0:
            current_leverage = portfolio.leverage
            if current_leverage > self.max_leverage:
                return False, f"Leverage {current_leverage:.2f}x exceeds max {self.max_leverage}x"

        # Check 4: Daily order count
        if self._daily_order_count >= self.max_daily_orders:
            return False, f"Daily order limit reached ({self.max_daily_orders})"

        # Check 5: Daily loss limit
        if portfolio.total_value > 0:
            daily_loss_pct = abs(min(portfolio.daily_pnl, 0)) / portfolio.total_value
            if daily_loss_pct > self.max_daily_loss_pct:
                return False, (
                    f"Daily loss {daily_loss_pct:.1%} "
                    f"exceeds max {self.max_daily_loss_pct:.1%}"
                )

        self._daily_order_count += 1
        return True, "approved"

    def reset_daily(self) -> None:
        """Reset daily counters (call at start of each trading day)."""
        self._daily_order_count = 0
        self._daily_pnl = 0.0
