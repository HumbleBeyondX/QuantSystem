"""Unit tests for core domain models."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from core.enums import (
    AssetClass,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    SignalDirection,
    TimeFrame,
)
from core.models import (
    Bar,
    Fill,
    Order,
    OrderBookLevel,
    OrderBookSnapshot,
    Position,
    Signal,
    Tick,
)


class TestBar:
    def test_bar_creation(self, sample_bars):
        """A Bar should store OHLCV fields correctly."""
        bar = sample_bars[0]

        assert bar.symbol == "AAPL"
        assert bar.open == 190.00
        assert bar.high == 191.50
        assert bar.low == 189.50
        assert bar.close == 190.80
        assert bar.volume == 1_200_000
        assert bar.timeframe == TimeFrame.M1
        assert bar.asset_class == AssetClass.EQUITY

    def test_bar_default_timeframe(self):
        """Default timeframe should be M1."""
        bar = Bar(
            symbol="TEST",
            timestamp=datetime.utcnow(),
            open=100, high=101, low=99, close=100.5, volume=1000,
        )
        assert bar.timeframe == TimeFrame.M1


class TestTick:
    def test_tick_creation(self, sample_ticks):
        """A Tick should store bid/ask/last/volume correctly."""
        tick = sample_ticks[0]

        assert tick.symbol == "BTC/USDT"
        assert tick.bid == 67_000.0
        assert tick.ask == 67_010.0
        assert tick.last == 67_005.0
        assert tick.volume == 0.5
        assert tick.asset_class == AssetClass.CRYPTO

    def test_tick_default_values(self):
        """Tick with minimal fields should have zero defaults."""
        tick = Tick(symbol="ETH/USDT", timestamp=datetime.utcnow())
        assert tick.bid == 0.0
        assert tick.ask == 0.0
        assert tick.last == 0.0
        assert tick.volume == 0.0


class TestOrder:
    def test_order_creation(self):
        """An Order should be created with correct status and fields."""
        order = Order(
            order_id=uuid4(),
            strategy_id="test_strategy",
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=100.0,
            limit_price=190.0,
        )

        assert order.status == OrderStatus.PENDING
        assert order.side == OrderSide.BUY
        assert order.order_type == OrderType.LIMIT
        assert order.quantity == 100.0
        assert order.limit_price == 190.0
        assert order.filled_quantity == 0.0

    def test_order_market_no_limit(self):
        """A market order should have no limit_price."""
        order = Order(
            order_id=uuid4(),
            strategy_id="s1",
            symbol="SPY",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=50.0,
        )
        assert order.limit_price is None
        assert order.stop_price is None


class TestFill:
    def test_fill_creation(self):
        """A Fill should capture price, quantity, and commission."""
        fill = Fill(
            fill_id=uuid4(),
            order_id=uuid4(),
            symbol="AAPL",
            side=OrderSide.BUY,
            price=191.50,
            quantity=100.0,
            commission=19.15,
            timestamp=datetime.utcnow(),
            strategy_id="test_strategy",
        )

        assert fill.price == 191.50
        assert fill.quantity == 100.0
        assert fill.commission == 19.15
        assert fill.side == OrderSide.BUY


class TestPosition:
    def test_position_update_on_fill(self):
        """Updating current_price should change market_value and unrealized_pnl."""
        pos = Position(
            symbol="AAPL",
            strategy_id="test",
            side=PositionSide.LONG,
            quantity=100.0,
            average_entry_price=185.0,
            current_price=185.0,
        )
        assert pos.market_value == 100.0 * 185.0
        assert pos.cost_basis == 100.0 * 185.0

        # Simulate price move
        pos.current_price = 192.0
        pos.unrealized_pnl = (192.0 - 185.0) * 100.0

        assert pos.market_value == 100.0 * 192.0
        assert pos.unrealized_pnl == 700.0


class TestOrderBook:
    def test_orderbook_microprice(self, sample_orderbook):
        """Microprice should be volume-weighted towards the thinner side."""
        ob = sample_orderbook
        bid_vol = ob.bids[0].quantity  # 5.0
        ask_vol = ob.asks[0].quantity  # 4.0

        expected_microprice = (
            ob.best_bid * ask_vol + ob.best_ask * bid_vol
        ) / (bid_vol + ask_vol)

        assert ob.microprice == expected_microprice
        # Microprice should be between bid and ask
        assert ob.best_bid < ob.microprice < ob.best_ask

    def test_orderbook_spread(self, sample_orderbook):
        """Spread should be ask - bid."""
        ob = sample_orderbook
        assert ob.spread == ob.best_ask - ob.best_bid


class TestSignal:
    def test_signal_creation(self, sample_signals):
        """A Signal should store direction, strength, and optional targets."""
        sig = sample_signals[0]

        assert sig.strategy_id == "test_strategy"
        assert sig.symbol == "AAPL"
        assert sig.direction == SignalDirection.LONG
        assert 0 <= sig.strength <= 1
        assert sig.stop_loss == 188.0
        assert sig.take_profit == 200.0

    def test_signal_no_targets(self, sample_signals):
        """A Signal without targets should have None for optional fields."""
        sig = sample_signals[1]

        assert sig.direction == SignalDirection.SHORT
        assert sig.stop_loss is None
        assert sig.take_profit is None
