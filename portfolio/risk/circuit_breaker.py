"""Circuit breaker: emergency stop for extreme conditions.

Three levels:
1. Strategy pause: individual strategy exceeds its loss limit
2. Portfolio halt: total portfolio loss exceeds threshold
3. System kill: data outage, broker disconnect, or manual trigger
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from core.logging import get_logger
from core.models import PortfolioSnapshot

logger = get_logger(__name__)


class CircuitBreaker:
    """Emergency stop system with multiple trigger levels."""

    def __init__(self, config: Dict[str, Any] = None) -> None:
        config = config or {}
        self.strategy_loss_limit_pct = config.get("strategy_loss_limit_pct", 0.03)  # 3%
        self.portfolio_loss_limit_pct = config.get("portfolio_loss_limit_pct", 0.05)  # 5%
        self.max_drawdown_kill_pct = config.get("max_drawdown_kill_pct", 0.20)  # 20%
        self.data_stale_threshold_sec = config.get("data_stale_threshold_sec", 120)
        self.cooldown_minutes = config.get("cooldown_minutes", 30)

        # State
        self._triggered = False
        self._trigger_time: datetime | None = None
        self._trigger_reason: str = ""
        self._initial_value: float = 0.0
        self._peak_value: float = 0.0

    def check(self, portfolio: PortfolioSnapshot) -> Dict[str, Any]:
        """Check all circuit breaker conditions.

        Returns: {triggered: bool, level: str, reason: str}
        """
        if self._initial_value == 0:
            self._initial_value = portfolio.total_value
        self._peak_value = max(self._peak_value, portfolio.total_value)

        value = portfolio.total_value

        # Level 1: Daily loss limit
        if self._initial_value > 0:
            daily_loss = (self._initial_value - value) / self._initial_value
            if daily_loss > self.portfolio_loss_limit_pct:
                return self._trigger(
                    "portfolio_halt",
                    f"Daily loss {daily_loss:.1%} exceeds {self.portfolio_loss_limit_pct:.1%}",
                )

        # Level 2: Max drawdown kill
        if self._peak_value > 0:
            drawdown = (self._peak_value - value) / self._peak_value
            if drawdown > self.max_drawdown_kill_pct:
                return self._trigger(
                    "max_drawdown_kill",
                    f"Drawdown {drawdown:.1%} exceeds {self.max_drawdown_kill_pct:.1%}",
                )

        # Level 3: Zero or negative portfolio value
        if value <= 0:
            return self._trigger("value_zero", "Portfolio value is zero or negative")

        return {"triggered": False, "level": "", "reason": ""}

    def check_strategy_loss(
        self, strategy_id: str, strategy_pnl: float, strategy_capital: float
    ) -> Dict[str, Any]:
        """Check if an individual strategy has exceeded its loss limit."""
        if strategy_capital > 0:
            loss_pct = abs(min(strategy_pnl, 0)) / strategy_capital
            if loss_pct > self.strategy_loss_limit_pct:
                return self._trigger(
                    "strategy_pause",
                    f"Strategy {strategy_id} loss {loss_pct:.1%} "
                    f"exceeds {self.strategy_loss_limit_pct:.1%}",
                )
        return {"triggered": False, "level": "", "reason": ""}

    def manual_kill(self, reason: str = "Manual kill switch activated") -> Dict[str, Any]:
        """Manually trigger the circuit breaker."""
        return self._trigger("manual_kill", reason)

    def _trigger(self, level: str, reason: str) -> Dict[str, Any]:
        self._triggered = True
        self._trigger_time = datetime.utcnow()
        self._trigger_reason = reason
        logger.critical("circuit_breaker_triggered", level=level, reason=reason)
        return {"triggered": True, "level": level, "reason": reason, "time": self._trigger_time}

    def reset(self) -> None:
        """Reset circuit breaker state."""
        self._triggered = False
        self._trigger_time = None
        self._trigger_reason = ""
        self._initial_value = 0.0
        logger.info("circuit_breaker_reset")

    def reset_daily(self) -> None:
        """Reset daily tracking (call at start of day)."""
        self._initial_value = 0.0

    @property
    def status(self) -> Dict[str, Any]:
        return {
            "triggered": self._triggered,
            "trigger_time": self._trigger_time,
            "trigger_reason": self._trigger_reason,
            "initial_value": self._initial_value,
            "peak_value": self._peak_value,
        }
