"""Audit logger — records all state-changing actions to the database.

Provides a structured audit trail for compliance and debugging.
All audit records are immutable (append-only).

Usage:
    from core.security.audit import audit_log

    await audit_log(
        session,
        action="order_submitted",
        entity_type="order",
        entity_id="abc-123",
        details={"symbol": "BTC/USDT", "side": "BUY", "qty": 0.5},
    )
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.events import Event, EventBus, EventType

logger = logging.getLogger(__name__)


async def audit_log(
    session,  # AsyncSession
    action: str,
    entity_type: str,
    entity_id: Optional[str] = None,
    user: str = "system",
    details: Optional[Dict[str, Any]] = None,
    ip_address: Optional[str] = None,
) -> None:
    """Write an audit log entry to the database.

    Args:
        session: SQLAlchemy AsyncSession (caller manages commit).
        action: What happened (e.g., "order_submitted", "strategy_started").
        entity_type: What type of entity (e.g., "order", "strategy", "position").
        entity_id: ID of the entity affected.
        user: Who performed the action (default "system").
        details: Additional context as a dict.
        ip_address: Remote IP if applicable.
    """
    from core.database.models import AuditLogRecord

    record = AuditLogRecord(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        user=user,
        details_json=json.dumps(details, default=str) if details else None,
        ip_address=ip_address,
        timestamp=datetime.now(timezone.utc),
    )
    session.add(record)
    # Do NOT commit — caller decides when to commit


class AuditEventHandler:
    """EventBus subscriber that automatically logs events to the audit trail.

    Subscribes to ORDER, FILL, RISK_ALERT, and STRATEGY_STATE events.
    """

    def __init__(self, event_bus: EventBus, get_session_fn):
        """
        Args:
            event_bus: The EventBus to subscribe to.
            get_session_fn: Async context manager that yields an AsyncSession.
        """
        self._get_session = get_session_fn

        event_bus.subscribe(EventType.ORDER, self._on_order)
        event_bus.subscribe(EventType.FILL, self._on_fill)
        event_bus.subscribe(EventType.RISK_ALERT, self._on_risk_alert)
        event_bus.subscribe(EventType.STRATEGY_STATE, self._on_strategy_state)

    async def _on_order(self, event: Event) -> None:
        try:
            async with self._get_session() as session:
                await audit_log(
                    session,
                    action="order_event",
                    entity_type="order",
                    entity_id=str(getattr(event, "order_id", "")),
                    details={
                        "strategy_id": getattr(event, "strategy_id", ""),
                        "symbol": getattr(event, "symbol", ""),
                        "side": str(getattr(event, "side", "")),
                        "quantity": getattr(event, "quantity", 0),
                    },
                )
                await session.commit()
        except Exception as e:
            logger.debug("audit_order_failed", error=str(e))

    async def _on_fill(self, event: Event) -> None:
        try:
            async with self._get_session() as session:
                await audit_log(
                    session,
                    action="fill_event",
                    entity_type="fill",
                    entity_id=str(getattr(event, "fill_id", "")),
                    details={
                        "order_id": str(getattr(event, "order_id", "")),
                        "symbol": getattr(event, "symbol", ""),
                        "side": str(getattr(event, "side", "")),
                        "price": getattr(event, "price", 0),
                        "quantity": getattr(event, "quantity", 0),
                    },
                )
                await session.commit()
        except Exception as e:
            logger.debug("audit_fill_failed", error=str(e))

    async def _on_risk_alert(self, event: Event) -> None:
        try:
            async with self._get_session() as session:
                await audit_log(
                    session,
                    action="risk_alert",
                    entity_type="risk",
                    details={
                        "level": getattr(event, "alert_level", ""),
                        "strategy_id": getattr(event, "strategy_id", ""),
                        "message": getattr(event, "message", ""),
                        "metric": getattr(event, "metric_name", ""),
                        "value": getattr(event, "metric_value", 0),
                    },
                )
                await session.commit()
        except Exception as e:
            logger.debug("audit_risk_failed", error=str(e))

    async def _on_strategy_state(self, event: Event) -> None:
        try:
            async with self._get_session() as session:
                await audit_log(
                    session,
                    action="strategy_state_change",
                    entity_type="strategy",
                    entity_id=getattr(event, "strategy_id", None),
                    details=event.payload if hasattr(event, "payload") else {},
                )
                await session.commit()
        except Exception as e:
            logger.debug("audit_strategy_state_failed", error=str(e))
