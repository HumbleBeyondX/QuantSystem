"""Unified notification manager — routes alerts to email, desktop, and WebSocket.

Subscribes to EventBus events and dispatches notifications based on
configurable triggers. Integrates with the existing EmailNotifier.

Usage:
    from core.notifications.notification_manager import NotificationManager

    nm = NotificationManager(event_bus)
    nm.enable_email()        # Uses existing EmailNotifier
    nm.enable_websocket(ws)  # Push to frontend via WebSocket
    await nm.start()
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional

from core.enums import StrategyState
from core.events import Event, EventBus, EventType, FillEvent, RiskAlertEvent

logger = logging.getLogger(__name__)


class NotificationLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Notification:
    """A notification to be dispatched to one or more channels."""
    title: str
    message: str
    level: NotificationLevel = NotificationLevel.INFO
    source: str = "system"
    strategy_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# Type for notification channels
NotificationChannel = Callable[[Notification], Coroutine[Any, Any, None]]


class NotificationManager:
    """Routes notifications to registered channels (email, desktop, WebSocket).

    Subscribes to EventBus for automatic notifications on:
    - Risk alerts (WARNING, CRITICAL, KILL)
    - Strategy state changes (ERROR, STOPPED unexpectedly)
    - Fill events (position opened/closed) — configurable
    """

    def __init__(
        self,
        event_bus: Optional[EventBus] = None,
        notify_on_fills: bool = False,
        notify_on_state_change: bool = True,
    ):
        self._event_bus = event_bus
        self._channels: List[NotificationChannel] = []
        self._notify_on_fills = notify_on_fills
        self._notify_on_state_change = notify_on_state_change
        self._queue: asyncio.Queue[Notification] = asyncio.Queue()
        self._dispatch_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Channel registration
    # ------------------------------------------------------------------

    def add_channel(self, channel: NotificationChannel) -> None:
        """Register a notification channel."""
        self._channels.append(channel)

    def enable_email(self) -> None:
        """Enable email notifications using the existing EmailNotifier."""
        try:
            from dashboard.alerts.email_notifier import get_email_notifier
            from dashboard.alerts.alert_manager import Alert, AlertSeverity

            notifier = get_email_notifier()
            if not notifier.is_configured:
                logger.warning("email_notifications_not_configured")
                return

            async def email_channel(notification: Notification) -> None:
                level_map = {
                    NotificationLevel.INFO: AlertSeverity.INFO,
                    NotificationLevel.WARNING: AlertSeverity.WARNING,
                    NotificationLevel.CRITICAL: AlertSeverity.CRITICAL,
                }
                alert = Alert(
                    title=notification.title,
                    message=notification.message,
                    severity=level_map.get(notification.level, AlertSeverity.INFO),
                    source=notification.source,
                    metadata=notification.metadata,
                )
                await notifier.send_alert(alert)

            self._channels.append(email_channel)
            logger.info("email_notification_channel_enabled")
        except Exception as e:
            logger.warning("email_channel_setup_failed", error=str(e))

    def enable_websocket(self, ws_manager: Any) -> None:
        """Enable WebSocket push notifications to the frontend."""
        async def ws_channel(notification: Notification) -> None:
            try:
                payload = {
                    "type": "notification",
                    "title": notification.title,
                    "message": notification.message,
                    "level": notification.level.value,
                    "source": notification.source,
                    "strategy_id": notification.strategy_id,
                    "timestamp": notification.timestamp.isoformat(),
                    "metadata": notification.metadata,
                }
                await ws_manager.broadcast(json.dumps(payload))
            except Exception as e:
                logger.debug("ws_notification_failed", error=str(e))

        self._channels.append(ws_channel)
        logger.info("websocket_notification_channel_enabled")

    # ------------------------------------------------------------------
    # Event handlers (subscribed to EventBus)
    # ------------------------------------------------------------------

    async def _on_risk_alert(self, event: Event) -> None:
        if not isinstance(event, RiskAlertEvent):
            return

        level_map = {
            "WARNING": NotificationLevel.WARNING,
            "CRITICAL": NotificationLevel.CRITICAL,
            "KILL": NotificationLevel.CRITICAL,
        }
        level = level_map.get(event.alert_level, NotificationLevel.WARNING)

        await self.notify(Notification(
            title=f"Risk Alert: {event.alert_level}",
            message=event.message,
            level=level,
            source="risk_manager",
            strategy_id=event.strategy_id or None,
            metadata={
                "metric": event.metric_name,
                "value": event.metric_value,
                "threshold": event.threshold,
            },
        ))

    async def _on_fill(self, event: Event) -> None:
        if not self._notify_on_fills or not isinstance(event, FillEvent):
            return

        side_str = "bought" if event.side.value == "BUY" else "sold"
        await self.notify(Notification(
            title=f"Fill: {side_str} {event.quantity} {event.symbol}",
            message=f"Order filled at ${event.price:.2f} (commission: ${event.commission:.2f})",
            level=NotificationLevel.INFO,
            source="execution",
            strategy_id=event.strategy_id or None,
        ))

    # ------------------------------------------------------------------
    # Strategy state change handler (called by orchestrator)
    # ------------------------------------------------------------------

    async def on_strategy_state_change(
        self,
        strategy_id: str,
        old_state: StrategyState,
        new_state: StrategyState,
    ) -> None:
        """Called by the orchestrator when a strategy changes state."""
        if not self._notify_on_state_change:
            return

        # Only notify on important transitions
        if new_state == StrategyState.ERROR:
            await self.notify(Notification(
                title=f"Strategy Error: {strategy_id}",
                message=f"Strategy {strategy_id} entered ERROR state (was {old_state.value})",
                level=NotificationLevel.CRITICAL,
                source="orchestrator",
                strategy_id=strategy_id,
            ))
        elif new_state == StrategyState.RUNNING and old_state in (StrategyState.IDLE, StrategyState.STOPPED):
            await self.notify(Notification(
                title=f"Strategy Started: {strategy_id}",
                message=f"Strategy {strategy_id} is now running",
                level=NotificationLevel.INFO,
                source="orchestrator",
                strategy_id=strategy_id,
            ))
        elif new_state == StrategyState.STOPPED and old_state == StrategyState.RUNNING:
            await self.notify(Notification(
                title=f"Strategy Stopped: {strategy_id}",
                message=f"Strategy {strategy_id} has been stopped",
                level=NotificationLevel.WARNING,
                source="orchestrator",
                strategy_id=strategy_id,
            ))

    # ------------------------------------------------------------------
    # Send notification
    # ------------------------------------------------------------------

    async def notify(self, notification: Notification) -> None:
        """Queue a notification for dispatch."""
        await self._queue.put(notification)

    async def _dispatch_loop(self) -> None:
        """Background loop dispatching queued notifications to all channels."""
        while True:
            try:
                notification = await self._queue.get()
                tasks = [ch(notification) for ch in self._channels]
                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for i, result in enumerate(results):
                        if isinstance(result, Exception):
                            logger.warning(
                                "notification_channel_error",
                                channel=i,
                                error=str(result),
                            )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("notification_dispatch_error", error=str(e))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the notification manager — subscribe to events and start dispatch."""
        if self._event_bus:
            self._event_bus.subscribe(EventType.RISK_ALERT, self._on_risk_alert)
            if self._notify_on_fills:
                self._event_bus.subscribe(EventType.FILL, self._on_fill)

        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        logger.info("notification_manager_started", channels=len(self._channels))

    async def stop(self) -> None:
        """Stop the notification manager."""
        if self._dispatch_task and not self._dispatch_task.done():
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
        logger.info("notification_manager_stopped")
