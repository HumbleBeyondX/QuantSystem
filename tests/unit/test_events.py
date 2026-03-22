"""Unit tests for the EventBus pub/sub system."""

from __future__ import annotations

import asyncio

import pytest

from core.events import (
    Event,
    EventBus,
    EventType,
    FillEvent,
    MarketDataEvent,
    RiskAlertEvent,
    SignalEvent,
)
from core.enums import AssetClass, DataType, OrderSide, SignalDirection


class TestEventCreation:
    def test_base_event_defaults(self):
        """Base Event should have a unique ID and default type."""
        e = Event()
        assert e.event_type == EventType.SYSTEM_HEALTH
        assert e.event_id is not None
        assert e.source == ""

    def test_market_data_event(self):
        """MarketDataEvent should auto-set event_type."""
        e = MarketDataEvent(symbol="BTCUSDT", asset_class=AssetClass.CRYPTO)
        assert e.event_type == EventType.MARKET_DATA
        assert e.symbol == "BTCUSDT"

    def test_signal_event(self):
        """SignalEvent should carry strategy_id and direction."""
        e = SignalEvent(
            strategy_id="crypto_scalp",
            symbol="ETHUSDT",
            direction=SignalDirection.LONG,
            strength=0.85,
        )
        assert e.event_type == EventType.SIGNAL
        assert e.strength == 0.85

    def test_fill_event(self):
        """FillEvent should carry fill details."""
        e = FillEvent(
            strategy_id="test",
            symbol="AAPL",
            side=OrderSide.BUY,
            price=192.50,
            quantity=100,
            commission=1.50,
        )
        assert e.event_type == EventType.FILL
        assert e.price == 192.50

    def test_events_are_frozen(self):
        """Events should be immutable."""
        e = Event()
        with pytest.raises(AttributeError):
            e.source = "modified"


class TestEventBus:
    @pytest.mark.asyncio
    async def test_subscribe_and_publish(self, event_bus: EventBus):
        """Handler should receive published events of matching type."""
        received = []

        async def handler(event: Event):
            received.append(event)

        event_bus.subscribe(EventType.SIGNAL, handler)
        sig = SignalEvent(strategy_id="s1", symbol="AAPL", strength=0.5)
        await event_bus.publish(sig)

        assert len(received) == 1
        assert received[0].symbol == "AAPL"

    @pytest.mark.asyncio
    async def test_handler_only_receives_subscribed_type(self, event_bus: EventBus):
        """Handler should NOT receive events of a different type."""
        received = []

        async def handler(event: Event):
            received.append(event)

        event_bus.subscribe(EventType.SIGNAL, handler)
        mkt = MarketDataEvent(symbol="BTCUSDT")
        await event_bus.publish(mkt)

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_subscribe_all(self, event_bus: EventBus):
        """subscribe_all should receive all event types."""
        received = []

        async def handler(event: Event):
            received.append(event)

        event_bus.subscribe_all(handler)
        await event_bus.publish(SignalEvent(strategy_id="s1", symbol="X"))
        await event_bus.publish(MarketDataEvent(symbol="Y"))

        assert len(received) == 2

    @pytest.mark.asyncio
    async def test_unsubscribe(self, event_bus: EventBus):
        """After unsubscribe, handler should stop receiving events."""
        received = []

        async def handler(event: Event):
            received.append(event)

        event_bus.subscribe(EventType.FILL, handler)
        await event_bus.publish(FillEvent(symbol="AAPL"))
        assert len(received) == 1

        event_bus.unsubscribe(EventType.FILL, handler)
        await event_bus.publish(FillEvent(symbol="MSFT"))
        assert len(received) == 1  # no new event

    @pytest.mark.asyncio
    async def test_multiple_handlers(self, event_bus: EventBus):
        """Multiple handlers for the same event type should all be called."""
        results = {"a": [], "b": []}

        async def handler_a(event):
            results["a"].append(event)

        async def handler_b(event):
            results["b"].append(event)

        event_bus.subscribe(EventType.RISK_ALERT, handler_a)
        event_bus.subscribe(EventType.RISK_ALERT, handler_b)

        alert = RiskAlertEvent(alert_level="WARNING", message="test")
        await event_bus.publish(alert)

        assert len(results["a"]) == 1
        assert len(results["b"]) == 1

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_block_others(self, event_bus: EventBus):
        """A failing handler should not prevent other handlers from executing."""
        received = []

        async def bad_handler(event):
            raise ValueError("intentional error")

        async def good_handler(event):
            received.append(event)

        event_bus.subscribe(EventType.SIGNAL, bad_handler)
        event_bus.subscribe(EventType.SIGNAL, good_handler)

        await event_bus.publish(SignalEvent(strategy_id="s1", symbol="X"))
        assert len(received) == 1
