"""Unit tests for execution engine, slippage, fees, and order management."""

from __future__ import annotations

import asyncio
from datetime import datetime
from uuid import uuid4

import pytest

from core.enums import AssetClass, OrderSide, OrderStatus, OrderType
from core.models import Order
from execution.engine.paper_engine import PaperEngine
from execution.models.fees import CryptoMakerTakerFee, IBFeeModel, PercentageFee, ZeroFee
from execution.models.slippage import FixedSlippage, ProportionalSlippage, ZeroSlippage
from execution.oms.order_manager import OrderManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_order(
    symbol: str = "AAPL",
    side: OrderSide = OrderSide.BUY,
    quantity: float = 100.0,
    order_type: OrderType = OrderType.MARKET,
    limit_price: float | None = None,
) -> Order:
    return Order(
        order_id=uuid4(),
        strategy_id="test",
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=quantity,
        limit_price=limit_price,
    )


# ---------------------------------------------------------------------------
# Paper Engine Tests
# ---------------------------------------------------------------------------


class TestPaperEngine:
    @pytest.mark.asyncio
    async def test_paper_engine_market_order(self, paper_engine: PaperEngine):
        """A market order should be filled immediately at the current price."""
        await paper_engine.connect()
        paper_engine.update_price("AAPL", 190.0)

        order = _make_order(symbol="AAPL", side=OrderSide.BUY, quantity=50.0)
        await paper_engine.submit_order(order)

        assert order.status == OrderStatus.FILLED
        assert order.filled_quantity == 50.0
        assert order.average_fill_price > 0

        fills = await paper_engine.get_fills()
        # fills were already consumed during submit_order -> _execute_fill
        # but internal positions should be updated
        positions = await paper_engine.get_positions()
        assert positions.get("AAPL", 0) == 50.0

        await paper_engine.disconnect()

    @pytest.mark.asyncio
    async def test_paper_engine_limit_order_fill(self, paper_engine: PaperEngine):
        """A buy limit order should fill when market price drops to the limit."""
        await paper_engine.connect()
        paper_engine.update_price("AAPL", 195.0)

        order = _make_order(
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=30.0,
            order_type=OrderType.LIMIT,
            limit_price=190.0,
        )
        await paper_engine.submit_order(order)

        # Should be pending (price 195 > limit 190)
        assert order.status == OrderStatus.SUBMITTED

        # Price drops to limit
        paper_engine.update_price("AAPL", 189.0)
        assert order.status == OrderStatus.FILLED
        assert order.filled_quantity == 30.0

        await paper_engine.disconnect()

    @pytest.mark.asyncio
    async def test_paper_engine_limit_order_no_fill(self, paper_engine: PaperEngine):
        """A buy limit order should remain pending if price stays above limit."""
        await paper_engine.connect()
        paper_engine.update_price("AAPL", 195.0)

        order = _make_order(
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=30.0,
            order_type=OrderType.LIMIT,
            limit_price=180.0,
        )
        await paper_engine.submit_order(order)

        # Price does not reach limit
        paper_engine.update_price("AAPL", 192.0)
        assert order.status == OrderStatus.SUBMITTED

        await paper_engine.disconnect()


# ---------------------------------------------------------------------------
# Slippage Model Tests
# ---------------------------------------------------------------------------


class TestSlippageModels:
    def test_fixed_slippage_buy(self):
        """Fixed slippage should increase the fill price for buys."""
        model = FixedSlippage(bps=10)
        fill_price = model.apply(100.0, OrderSide.BUY, 50.0)
        assert fill_price > 100.0
        assert abs(fill_price - 100.10) < 1e-6  # 10 bps = 0.10%

    def test_fixed_slippage_sell(self):
        """Fixed slippage should decrease the fill price for sells."""
        model = FixedSlippage(bps=10)
        fill_price = model.apply(100.0, OrderSide.SELL, 50.0)
        assert fill_price < 100.0

    def test_proportional_slippage(self):
        """Proportional slippage should increase with order size."""
        model = ProportionalSlippage(base_bps=2, impact_factor=0.1)
        small_slip = model.apply(100.0, OrderSide.BUY, 10.0)
        large_slip = model.apply(100.0, OrderSide.BUY, 10_000.0)
        assert large_slip > small_slip

    def test_zero_slippage(self):
        """Zero slippage should return the exact input price."""
        model = ZeroSlippage()
        assert model.apply(100.0, OrderSide.BUY, 50.0) == 100.0
        assert model.apply(100.0, OrderSide.SELL, 50.0) == 100.0


# ---------------------------------------------------------------------------
# Fee Model Tests
# ---------------------------------------------------------------------------


class TestFeeModels:
    def test_percentage_fee(self):
        """PercentageFee should compute price * quantity * rate."""
        model = PercentageFee(rate=0.001)
        fee = model.compute(100.0, 50.0)
        assert abs(fee - 5.0) < 1e-6  # 0.1% of $5000

    def test_ib_fee_model(self):
        """IB fee should be at least $0.35 and at most 1% of trade value."""
        model = IBFeeModel()
        # Small order: $0.0035 * 10 = $0.035 < min $0.35
        fee_small = model.compute(50.0, 10.0)
        assert fee_small == 0.35

        # Normal order
        fee_normal = model.compute(200.0, 500.0)
        assert fee_normal == 500 * 0.0035  # $1.75

    def test_crypto_maker_taker(self):
        """CryptoMakerTakerFee should apply maker rate for maker orders."""
        maker = CryptoMakerTakerFee(maker=0.0002, taker=0.0005, is_maker=True)
        taker = CryptoMakerTakerFee(maker=0.0002, taker=0.0005, is_maker=False)

        fee_maker = maker.compute(67_000.0, 1.0)
        fee_taker = taker.compute(67_000.0, 1.0)

        assert fee_maker < fee_taker
        assert abs(fee_maker - 67_000 * 0.0002) < 1e-6

    def test_zero_fee(self):
        """ZeroFee should always return 0."""
        model = ZeroFee()
        assert model.compute(1000.0, 500.0) == 0.0


# ---------------------------------------------------------------------------
# Order Manager Tests
# ---------------------------------------------------------------------------


class TestOrderManager:
    def test_order_manager_create(self):
        """OrderManager.create_order should store and return a valid Order."""
        om = OrderManager()
        order = om.create_order(
            strategy_id="test",
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=100.0,
            order_type=OrderType.MARKET,
        )

        assert order.symbol == "AAPL"
        assert order.status == OrderStatus.PENDING
        assert om.get_order(order.order_id) is order

    def test_order_manager_pending_list(self):
        """get_pending_orders should return only pending/submitted orders."""
        om = OrderManager()
        o1 = om.create_order("s1", "AAPL", OrderSide.BUY, 10.0)
        o2 = om.create_order("s1", "MSFT", OrderSide.SELL, 20.0)

        pending = om.get_pending_orders()
        assert len(pending) == 2

        om.update_status(o1.order_id, OrderStatus.FILLED)
        pending = om.get_pending_orders()
        assert len(pending) == 1
        assert pending[0].symbol == "MSFT"

    def test_order_manager_cancel_all(self):
        """cancel_all_pending should cancel every pending order."""
        om = OrderManager()
        om.create_order("s1", "AAPL", OrderSide.BUY, 10.0)
        om.create_order("s1", "MSFT", OrderSide.SELL, 20.0)

        cancelled = om.cancel_all_pending()
        assert len(cancelled) == 2
        assert om.get_pending_orders() == []
