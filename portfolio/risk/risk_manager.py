"""Risk manager orchestrator: coordinates pre-trade, real-time, and circuit breaker checks."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from core.logging import get_logger
from core.models import Order, Position, PortfolioSnapshot

logger = get_logger(__name__)


class RiskManager:
    """Central risk management orchestrator.

    Three-layer risk architecture:
    1. Pre-trade: validate orders before submission
    2. Real-time: monitor portfolio exposure and drawdowns
    3. Circuit breaker: emergency stop for extreme conditions
    """

    def __init__(self, config: Optional[Dict] = None) -> None:
        from portfolio.risk.pre_trade import PreTradeRiskCheck
        from portfolio.risk.realtime_monitor import RealtimeRiskMonitor
        from portfolio.risk.circuit_breaker import CircuitBreaker

        self.config = config or {}
        self.pre_trade = PreTradeRiskCheck(self.config.get("pre_trade", {}))
        self.realtime = RealtimeRiskMonitor(self.config.get("realtime", {}))
        self.circuit_breaker = CircuitBreaker(self.config.get("circuit_breaker", {}))
        self._is_halted = False

    def check_order(self, order: Order, portfolio: PortfolioSnapshot) -> tuple[bool, str]:
        """Pre-trade risk check for a single order.

        Returns: (approved, reason)
        """
        if self._is_halted:
            return False, "System halted by circuit breaker"

        return self.pre_trade.check(order, portfolio)

    def update(self, portfolio: PortfolioSnapshot) -> List[Dict[str, Any]]:
        """Real-time risk update. Returns list of risk alerts."""
        alerts = []

        # Real-time monitoring
        rt_alerts = self.realtime.update(portfolio)
        alerts.extend(rt_alerts)

        # Circuit breaker check
        cb_result = self.circuit_breaker.check(portfolio)
        if cb_result["triggered"]:
            self._is_halted = True
            alerts.append(cb_result)
            logger.critical("circuit_breaker_triggered", **cb_result)

        return alerts

    def reset_halt(self) -> None:
        """Manually reset the circuit breaker halt."""
        self._is_halted = False
        self.circuit_breaker.reset()
        logger.info("circuit_breaker_reset")

    @property
    def is_halted(self) -> bool:
        return self._is_halted

    def get_risk_summary(self, portfolio: PortfolioSnapshot) -> Dict[str, Any]:
        """Get comprehensive risk summary."""
        return {
            "is_halted": self._is_halted,
            "realtime": self.realtime.get_metrics(portfolio),
            "circuit_breaker": self.circuit_breaker.status,
        }
