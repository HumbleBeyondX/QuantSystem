"""Event bus and event types for inter-layer communication.

The event bus supports both in-process async pub/sub and cross-process
communication via Kafka topics. All events are immutable dataclasses.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Type
from uuid import UUID, uuid4

from core.enums import AssetClass, DataType, OrderSide, OrderType, SignalDirection


# ============================================================
# Event Types
# ============================================================


class EventType(str, Enum):
    """Categories of events flowing through the system."""

    MARKET_DATA = "market_data"          # L0 -> L1 -> L3
    FEATURE_UPDATE = "feature_update"    # L1 -> L3
    SIGNAL = "signal"                    # L3 -> L4
    TARGET_POSITION = "target_position"  # L4 -> L5
    ORDER = "order"                      # L5 internal
    FILL = "fill"                        # L5 -> L4, L6
    RISK_ALERT = "risk_alert"            # L4 -> L5, L6
    SYSTEM_HEALTH = "system_health"      # Any -> L6
    STRATEGY_STATE = "strategy_state"    # L3 -> L6


# ============================================================
# Event Dataclasses
# ============================================================


@dataclass(frozen=True, slots=True)
class Event:
    """Base event class. All events are immutable."""

    event_id: UUID = field(default_factory=uuid4)
    event_type: EventType = EventType.SYSTEM_HEALTH
    timestamp: datetime = field(default_factory=datetime.utcnow)
    source: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MarketDataEvent(Event):
    """Market data arriving from a data source."""

    event_type: EventType = field(default=EventType.MARKET_DATA, init=False)
    symbol: str = ""
    asset_class: AssetClass = AssetClass.CRYPTO
    data_type: DataType = DataType.TICK
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FeatureUpdateEvent(Event):
    """Computed feature/indicator values."""

    event_type: EventType = field(default=EventType.FEATURE_UPDATE, init=False)
    symbol: str = ""
    features: Dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SignalEvent(Event):
    """Trading signal from a strategy."""

    event_type: EventType = field(default=EventType.SIGNAL, init=False)
    strategy_id: str = ""
    symbol: str = ""
    direction: SignalDirection = SignalDirection.FLAT
    strength: float = 0.0
    target_quantity: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


@dataclass(frozen=True, slots=True)
class TargetPositionEvent(Event):
    """Target position from portfolio optimizer."""

    event_type: EventType = field(default=EventType.TARGET_POSITION, init=False)
    strategy_id: str = ""
    targets: Dict[str, float] = field(default_factory=dict)  # symbol -> target_qty


@dataclass(frozen=True, slots=True)
class OrderEvent(Event):
    """Order submitted to execution layer."""

    event_type: EventType = field(default=EventType.ORDER, init=False)
    order_id: UUID = field(default_factory=uuid4)
    strategy_id: str = ""
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    order_type: OrderType = OrderType.MARKET
    quantity: float = 0.0
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None


@dataclass(frozen=True, slots=True)
class FillEvent(Event):
    """Execution fill from broker."""

    event_type: EventType = field(default=EventType.FILL, init=False)
    fill_id: UUID = field(default_factory=uuid4)
    order_id: UUID = field(default_factory=uuid4)
    strategy_id: str = ""
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    price: float = 0.0
    quantity: float = 0.0
    commission: float = 0.0


@dataclass(frozen=True, slots=True)
class RiskAlertEvent(Event):
    """Risk alert from risk management layer."""

    event_type: EventType = field(default=EventType.RISK_ALERT, init=False)
    alert_level: str = "WARNING"  # WARNING | CRITICAL | KILL
    strategy_id: str = ""
    message: str = ""
    metric_name: str = ""
    metric_value: float = 0.0
    threshold: float = 0.0


# ============================================================
# Event Bus (In-Process Async Pub/Sub)
# ============================================================

# Type alias for event handler functions
EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """In-process async event bus for single-process communication.

    For cross-process communication, events are serialized to Kafka.
    The EventBus handles routing within a single Python process.
    """

    def __init__(self) -> None:
        self._handlers: Dict[EventType, List[EventHandler]] = {}
        self._global_handlers: List[EventHandler] = []

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Subscribe a handler to a specific event type."""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        """Subscribe a handler to all event types (e.g., for logging/audit)."""
        self._global_handlers.append(handler)

    def unsubscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Remove a handler from an event type."""
        if event_type in self._handlers:
            self._handlers[event_type] = [
                h for h in self._handlers[event_type] if h is not handler
            ]

    async def publish(self, event: Event) -> None:
        """Publish an event to all subscribed handlers."""
        handlers = list(self._global_handlers)
        handlers.extend(self._handlers.get(event.event_type, []))

        if handlers:
            await asyncio.gather(
                *(h(event) for h in handlers),
                return_exceptions=True,
            )

    async def publish_nowait(self, event: Event) -> None:
        """Publish an event without waiting for handlers to complete."""
        handlers = list(self._global_handlers)
        handlers.extend(self._handlers.get(event.event_type, []))

        for handler in handlers:
            asyncio.create_task(handler(event))
