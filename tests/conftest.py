"""Shared pytest fixtures for the QuantSystem test suite."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from core.enums import (
    AssetClass,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    SignalDirection,
    TimeFrame,
)
from core.events import EventBus
from core.models import (
    Bar,
    Fill,
    Order,
    OrderBookLevel,
    OrderBookSnapshot,
    Position,
    PortfolioSnapshot,
    Signal,
    Tick,
)
from data.features.feature_store import FeatureStore
from data.features.technical import register_technical_features
from execution.engine.paper_engine import PaperEngine
from execution.models.fees import PercentageFee
from execution.models.slippage import FixedSlippage

# ---------------------------------------------------------------------------
# Market Data Fixtures
# ---------------------------------------------------------------------------

_BASE_TS = datetime(2024, 6, 1, 9, 30, 0)


@pytest.fixture
def sample_bars() -> list[Bar]:
    """Return a list of 10 realistic OHLCV bars for AAPL."""
    prices = [
        (190.00, 191.50, 189.50, 190.80, 1_200_000),
        (190.80, 192.00, 190.20, 191.50, 1_350_000),
        (191.50, 193.00, 191.00, 192.80, 1_500_000),
        (192.80, 193.50, 191.80, 192.00, 1_100_000),
        (192.00, 192.50, 190.50, 191.00, 1_400_000),
        (191.00, 191.80, 189.50, 190.00, 1_600_000),
        (190.00, 191.00, 189.00, 190.50, 1_300_000),
        (190.50, 192.00, 190.00, 191.80, 1_250_000),
        (191.80, 193.20, 191.50, 193.00, 1_800_000),
        (193.00, 194.00, 192.50, 193.50, 2_000_000),
    ]
    return [
        Bar(
            symbol="AAPL",
            timestamp=_BASE_TS + timedelta(minutes=i),
            open=o,
            high=h,
            low=l,
            close=c,
            volume=v,
            timeframe=TimeFrame.M1,
            source="test",
            asset_class=AssetClass.EQUITY,
        )
        for i, (o, h, l, c, v) in enumerate(prices)
    ]


@pytest.fixture
def sample_ticks() -> list[Tick]:
    """Return a list of 5 ticks for BTC/USDT."""
    data = [
        (67_000.0, 67_010.0, 67_005.0, 0.5),
        (67_010.0, 67_020.0, 67_015.0, 1.2),
        (67_015.0, 67_025.0, 67_020.0, 0.8),
        (67_020.0, 67_035.0, 67_030.0, 2.0),
        (67_025.0, 67_040.0, 67_035.0, 0.3),
    ]
    return [
        Tick(
            symbol="BTC/USDT",
            timestamp=_BASE_TS + timedelta(seconds=i),
            bid=bid,
            ask=ask,
            last=last,
            volume=vol,
            source="test",
            asset_class=AssetClass.CRYPTO,
        )
        for i, (bid, ask, last, vol) in enumerate(data)
    ]


@pytest.fixture
def sample_orderbook() -> OrderBookSnapshot:
    """Return a realistic order book snapshot."""
    bids = [
        OrderBookLevel(price=67_000.0, quantity=5.0),
        OrderBookLevel(price=66_990.0, quantity=8.0),
        OrderBookLevel(price=66_980.0, quantity=12.0),
    ]
    asks = [
        OrderBookLevel(price=67_010.0, quantity=4.0),
        OrderBookLevel(price=67_020.0, quantity=7.0),
        OrderBookLevel(price=67_030.0, quantity=10.0),
    ]
    return OrderBookSnapshot(
        symbol="BTC/USDT",
        timestamp=_BASE_TS,
        bids=bids,
        asks=asks,
        source="test",
    )


@pytest.fixture
def sample_signals() -> list[Signal]:
    """Return a small set of trading signals."""
    return [
        Signal(
            signal_id=uuid4(),
            strategy_id="test_strategy",
            symbol="AAPL",
            direction=SignalDirection.LONG,
            strength=0.75,
            timestamp=_BASE_TS,
            target_price=192.0,
            stop_loss=188.0,
            take_profit=200.0,
        ),
        Signal(
            signal_id=uuid4(),
            strategy_id="test_strategy",
            symbol="MSFT",
            direction=SignalDirection.SHORT,
            strength=0.60,
            timestamp=_BASE_TS + timedelta(minutes=1),
        ),
    ]


@pytest.fixture
def sample_portfolio() -> PortfolioSnapshot:
    """Return a portfolio snapshot with two positions."""
    positions = [
        Position(
            symbol="AAPL",
            strategy_id="test_strategy",
            side=PositionSide.LONG,
            quantity=100.0,
            average_entry_price=185.0,
            current_price=192.0,
            unrealized_pnl=700.0,
            asset_class=AssetClass.EQUITY,
        ),
        Position(
            symbol="MSFT",
            strategy_id="test_strategy",
            side=PositionSide.LONG,
            quantity=50.0,
            average_entry_price=410.0,
            current_price=420.0,
            unrealized_pnl=500.0,
            asset_class=AssetClass.EQUITY,
        ),
    ]
    return PortfolioSnapshot(
        timestamp=_BASE_TS,
        total_value=150_000.0,
        cash=100_000.0,
        positions=positions,
        unrealized_pnl=1_200.0,
        realized_pnl=500.0,
        daily_pnl=300.0,
    )


# ---------------------------------------------------------------------------
# Infrastructure Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def event_bus() -> EventBus:
    """Return a fresh EventBus instance."""
    return EventBus()


@pytest.fixture
def paper_engine() -> PaperEngine:
    """Return a PaperEngine configured with modest slippage/fees."""
    return PaperEngine(
        slippage_model=FixedSlippage(bps=5),
        fee_model=PercentageFee(rate=0.001),
        fill_delay_ms=0,
    )


@pytest.fixture
def feature_store() -> FeatureStore:
    """Return a FeatureStore pre-loaded with technical indicators."""
    store = FeatureStore()
    register_technical_features(store)
    return store
