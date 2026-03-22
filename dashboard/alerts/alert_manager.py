"""Alert manager: routes alerts to configured notification channels."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional

from core.logging import get_logger

logger = get_logger(__name__)


class AlertSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertChannel(Enum):
    TELEGRAM = "telegram"
    DISCORD = "discord"
    LOG = "log"


@dataclass
class Alert:
    """A system alert to be dispatched to notification channels."""

    title: str
    message: str
    severity: AlertSeverity
    source: str  # e.g., "risk_manager", "circuit_breaker", "strategy"
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)


# Type for async alert handler
AlertHandler = Callable[[Alert], Coroutine[Any, Any, None]]


class AlertManager:
    """Routes alerts to registered notification channels."""

    def __init__(self) -> None:
        self._handlers: Dict[AlertChannel, AlertHandler] = {}
        self._severity_filter: AlertSeverity = AlertSeverity.INFO
        self._history: List[Alert] = []
        self._max_history: int = 1000
        self._rate_limits: Dict[str, datetime] = {}
        self._rate_limit_seconds: float = 60.0

    def register_handler(
        self, channel: AlertChannel, handler: AlertHandler
    ) -> None:
        """Register a notification handler for a channel."""
        self._handlers[channel] = handler
        logger.info("alert_handler_registered", channel=channel.value)

    def set_severity_filter(self, severity: AlertSeverity) -> None:
        """Only dispatch alerts at or above this severity level."""
        self._severity_filter = severity

    def set_rate_limit(self, seconds: float) -> None:
        """Minimum seconds between alerts with the same title."""
        self._rate_limit_seconds = seconds

    async def send(self, alert: Alert) -> None:
        """Dispatch alert to all registered channels."""
        if not self._should_send(alert):
            return

        self._history.append(alert)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

        self._rate_limits[alert.title] = alert.timestamp

        # Always log
        log_method = {
            AlertSeverity.INFO: logger.info,
            AlertSeverity.WARNING: logger.warning,
            AlertSeverity.CRITICAL: logger.error,
        }.get(alert.severity, logger.info)
        log_method(
            "alert_dispatched",
            title=alert.title,
            severity=alert.severity.value,
            source=alert.source,
        )

        # Send to all registered handlers concurrently
        tasks = []
        for channel, handler in self._handlers.items():
            tasks.append(self._safe_send(channel, handler, alert))

        if tasks:
            await asyncio.gather(*tasks)

    async def _safe_send(
        self, channel: AlertChannel, handler: AlertHandler, alert: Alert
    ) -> None:
        """Send to a single channel with error handling."""
        try:
            await handler(alert)
        except Exception as e:
            logger.error(
                "alert_send_failed",
                channel=channel.value,
                error=str(e),
            )

    def _should_send(self, alert: Alert) -> bool:
        """Check severity filter and rate limit."""
        severity_order = [AlertSeverity.INFO, AlertSeverity.WARNING, AlertSeverity.CRITICAL]
        if severity_order.index(alert.severity) < severity_order.index(self._severity_filter):
            return False

        last_sent = self._rate_limits.get(alert.title)
        if last_sent:
            elapsed = (alert.timestamp - last_sent).total_seconds()
            if elapsed < self._rate_limit_seconds:
                return False

        return True

    def get_recent_alerts(
        self, count: int = 50, severity: Optional[AlertSeverity] = None
    ) -> List[Alert]:
        """Get recent alert history."""
        alerts = self._history
        if severity:
            alerts = [a for a in alerts if a.severity == severity]
        return alerts[-count:]

    # ── Convenience methods ───────────────────────────────────

    async def info(self, title: str, message: str, source: str = "system") -> None:
        await self.send(Alert(title=title, message=message, severity=AlertSeverity.INFO, source=source))

    async def warning(self, title: str, message: str, source: str = "system") -> None:
        await self.send(Alert(title=title, message=message, severity=AlertSeverity.WARNING, source=source))

    async def critical(self, title: str, message: str, source: str = "system") -> None:
        await self.send(Alert(title=title, message=message, severity=AlertSeverity.CRITICAL, source=source))
