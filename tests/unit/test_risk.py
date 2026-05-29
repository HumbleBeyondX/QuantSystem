"""Unit tests for risk management: pre-trade checks, circuit breaker, VaR, drawdown."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import numpy as np
import pytest

from core.enums import AssetClass, OrderSide, OrderType, PositionSide
from core.models import Order, PortfolioSnapshot, Position
from portfolio.risk.circuit_breaker import CircuitBreaker
from portfolio.risk.pre_trade import PreTradeRiskCheck
from portfolio.risk.realtime_monitor import RealtimeRiskMonitor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_order(
    symbol: str = "AAPL",
    side: OrderSide = OrderSide.BUY,
    quantity: float = 100.0,
    limit_price: float | None = 200.0,
) -> Order:
    return Order(
        order_id=uuid4(),
        strategy_id="test",
        symbol=symbol,
        side=side,
        order_type=OrderType.LIMIT if limit_price else OrderType.MARKET,
        quantity=quantity,
        limit_price=limit_price,
    )


def _make_portfolio(
    total_value: float = 100_000.0,
    cash: float = 50_000.0,
    daily_pnl: float = 0.0,
    positions: list[Position] | None = None,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        timestamp=datetime.utcnow(),
        total_value=total_value,
        cash=cash,
        positions=positions or [],
        daily_pnl=daily_pnl,
    )


# ---------------------------------------------------------------------------
# Pre-Trade Risk Checks
# ---------------------------------------------------------------------------


class TestPreTradeRisk:
    def test_pre_trade_max_order_value(self):
        """Orders exceeding max_order_value should be rejected."""
        checker = PreTradeRiskCheck({"max_order_value": 10_000})
        order = _make_order(quantity=200, limit_price=100.0)  # value = 20,000
        portfolio = _make_portfolio()

        approved, reason = checker.check(order, portfolio)
        assert approved is False
        assert "exceeds max" in reason

    def test_pre_trade_approved(self):
        """Small orders within limits should be approved."""
        checker = PreTradeRiskCheck({"max_order_value": 50_000})
        order = _make_order(quantity=10, limit_price=100.0)  # value = 1,000
        portfolio = _make_portfolio()

        approved, reason = checker.check(order, portfolio)
        assert approved is True
        assert reason == "approved"

    def test_pre_trade_leverage_limit(self):
        """Orders that would exceed leverage should be rejected."""
        # Spread positions across multiple symbols to avoid concentration limit
        positions = [
            Position(
                symbol=f"STOCK_{i}",
                strategy_id="test",
                side=PositionSide.LONG,
                quantity=200.0,
                average_entry_price=200.0,
                current_price=200.0,
            )
            for i in range(5)
        ]
        # total_value = 100k, invested = 5*200*200 = 200k => leverage = 2.0
        portfolio = _make_portfolio(
            total_value=100_000.0,
            positions=positions,
        )
        checker = PreTradeRiskCheck({"max_leverage": 1.5, "max_position_pct": 0.50})

        order = _make_order(symbol="STOCK_6", quantity=10, limit_price=200.0)
        approved, reason = checker.check(order, portfolio)
        assert approved is False
        assert "Leverage" in reason

    def test_pre_trade_daily_loss_limit(self):
        """Orders should be rejected when daily loss exceeds threshold."""
        checker = PreTradeRiskCheck({"max_daily_loss_pct": 0.02})
        portfolio = _make_portfolio(
            total_value=100_000.0,
            daily_pnl=-3_000.0,  # -3% loss
        )
        order = _make_order(quantity=1, limit_price=100.0)

        approved, reason = checker.check(order, portfolio)
        assert approved is False
        assert "Daily loss" in reason


def _make_position(
    symbol: str = "ETH/USDT:USDT",
    side: PositionSide = PositionSide.LONG,
    quantity: float = 0.1,
    avg_price: float = 2252.97,
    current_price: float = 2018.0,
) -> Position:
    return Position(
        symbol=symbol,
        strategy_id="test",
        side=side,
        quantity=quantity,
        average_entry_price=avg_price,
        current_price=current_price,
        asset_class=AssetClass.CRYPTO,
    )


# ---------------------------------------------------------------------------
# Risk-reducing orders — regression guard for the 2026-05-29 "risk backfire"
# bug, where a stop-loss SELL was rejected by the concentration check and the
# losing position was trapped past its stop. Closing / trimming a position
# must ALWAYS be allowed regardless of concentration / leverage / daily-loss.
# ---------------------------------------------------------------------------


class TestRiskReducingOrders:
    def test_sell_closing_long_always_approved(self):
        """The exact regression: an over-concentrated LONG, market SELL to exit.
        Must be approved even though concentration > max."""
        checker = PreTradeRiskCheck({"max_position_pct": 0.50, "max_leverage": 55})
        pos = _make_position(side=PositionSide.LONG, quantity=0.1)
        portfolio = _make_portfolio(total_value=85.92, cash=60.0, positions=[pos])

        # Market SELL (no limit price) to close — this was being rejected
        order = _make_order(
            symbol="ETH/USDT:USDT", side=OrderSide.SELL,
            quantity=0.1, limit_price=None,
        )
        approved, reason = checker.check(order, portfolio)
        assert approved is True
        assert "risk-reducing" in reason

    def test_buy_closing_short_always_approved(self):
        """A BUY that covers a SHORT lowers exposure → always approved."""
        checker = PreTradeRiskCheck({"max_position_pct": 0.50})
        pos = _make_position(side=PositionSide.SHORT, quantity=0.1)
        portfolio = _make_portfolio(total_value=85.92, cash=60.0, positions=[pos])

        order = _make_order(
            symbol="ETH/USDT:USDT", side=OrderSide.BUY,
            quantity=0.1, limit_price=None,
        )
        approved, reason = checker.check(order, portfolio)
        assert approved is True
        assert "risk-reducing" in reason

    def test_sell_closing_long_bypasses_daily_loss(self):
        """Even with the daily-loss limit breached, a closing SELL must pass —
        otherwise the position is trapped exactly when you most need to exit."""
        checker = PreTradeRiskCheck({"max_daily_loss_pct": 0.02})
        pos = _make_position(side=PositionSide.LONG, quantity=0.1)
        portfolio = _make_portfolio(
            total_value=100_000.0, daily_pnl=-5_000.0, positions=[pos]  # -5% day
        )
        order = _make_order(
            symbol="ETH/USDT:USDT", side=OrderSide.SELL, quantity=0.1, limit_price=None
        )
        approved, reason = checker.check(order, portfolio)
        assert approved is True
        assert "risk-reducing" in reason

    def test_sell_closing_long_bypasses_leverage(self):
        """A closing SELL must pass even when portfolio leverage exceeds max."""
        # Build an over-levered book, then try to trim the ETH position.
        eth = _make_position(symbol="ETH/USDT:USDT", side=PositionSide.LONG,
                             quantity=1.0, avg_price=2000.0, current_price=2000.0)
        others = [
            Position(symbol=f"S{i}", strategy_id="test", side=PositionSide.LONG,
                     quantity=200.0, average_entry_price=200.0, current_price=200.0)
            for i in range(5)
        ]
        portfolio = _make_portfolio(total_value=100_000.0, positions=[eth, *others])
        checker = PreTradeRiskCheck({"max_leverage": 1.5, "max_position_pct": 0.99})

        order = _make_order(symbol="ETH/USDT:USDT", side=OrderSide.SELL,
                            quantity=0.5, limit_price=None)
        approved, reason = checker.check(order, portfolio)
        assert approved is True
        assert "risk-reducing" in reason

    def test_buy_adding_to_long_still_blocked(self):
        """A BUY that ADDS to a LONG increases risk → must remain subject to
        the concentration limit (the fix must not over-reach)."""
        checker = PreTradeRiskCheck({"max_position_pct": 0.50})
        pos = _make_position(side=PositionSide.LONG, quantity=0.1)
        portfolio = _make_portfolio(total_value=85.92, cash=60.0, positions=[pos])

        order = _make_order(
            symbol="ETH/USDT:USDT", side=OrderSide.BUY,
            quantity=0.1, limit_price=2018.0,  # adds ~$200 to a $86 book
        )
        approved, reason = checker.check(order, portfolio)
        assert approved is False
        assert "concentration" in reason.lower()

    def test_sell_adding_to_short_still_blocked(self):
        """A SELL that ADDS to a SHORT increases exposure → still limited."""
        checker = PreTradeRiskCheck({"max_position_pct": 0.50})
        pos = _make_position(side=PositionSide.SHORT, quantity=0.1)
        portfolio = _make_portfolio(total_value=85.92, cash=60.0, positions=[pos])

        order = _make_order(
            symbol="ETH/USDT:USDT", side=OrderSide.SELL,
            quantity=0.1, limit_price=2018.0,
        )
        approved, reason = checker.check(order, portfolio)
        assert approved is False
        assert "concentration" in reason.lower()

    def test_open_new_position_not_treated_as_reducing(self):
        """An order on a symbol with no existing position is NOT risk-reducing;
        it must pass through the normal checks."""
        checker = PreTradeRiskCheck({"max_order_value": 10_000})
        portfolio = _make_portfolio(positions=[])  # flat

        # Oversized opening order → should be rejected by order-value check
        order = _make_order(symbol="NEWSYM", side=OrderSide.SELL,
                            quantity=200, limit_price=100.0)  # $20k
        approved, reason = checker.check(order, portfolio)
        assert approved is False
        assert "exceeds max" in reason


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_circuit_breaker_activation(self):
        """Circuit breaker should trigger when portfolio drops below daily loss limit."""
        cb = CircuitBreaker({"portfolio_loss_limit_pct": 0.05})

        # First check sets initial value
        portfolio_start = _make_portfolio(total_value=100_000.0)
        result = cb.check(portfolio_start)
        assert result["triggered"] is False

        # Simulate 6% loss
        portfolio_down = _make_portfolio(total_value=94_000.0)
        result = cb.check(portfolio_down)
        assert result["triggered"] is True
        assert result["level"] == "portfolio_halt"

    def test_circuit_breaker_max_drawdown(self):
        """Circuit breaker should trigger on max drawdown from peak."""
        cb = CircuitBreaker({"max_drawdown_kill_pct": 0.10})

        # Set initial and push to a peak
        cb.check(_make_portfolio(total_value=100_000.0))
        cb.check(_make_portfolio(total_value=120_000.0))

        # Drop 15% from peak (120k -> 102k)
        result = cb.check(_make_portfolio(total_value=102_000.0))
        assert result["triggered"] is True
        assert result["level"] == "max_drawdown_kill"

    def test_circuit_breaker_no_trigger(self):
        """Circuit breaker should not trigger under normal conditions."""
        cb = CircuitBreaker()

        cb.check(_make_portfolio(total_value=100_000.0))
        result = cb.check(_make_portfolio(total_value=99_000.0))
        assert result["triggered"] is False

    def test_circuit_breaker_reset(self):
        """After reset, the circuit breaker should accept new trades."""
        cb = CircuitBreaker({"portfolio_loss_limit_pct": 0.01})
        cb.check(_make_portfolio(total_value=100_000.0))
        cb.check(_make_portfolio(total_value=95_000.0))  # triggers

        cb.reset()
        assert cb.status["triggered"] is False


# ---------------------------------------------------------------------------
# VaR and Drawdown
# ---------------------------------------------------------------------------


class TestRealtimeRiskMonitor:
    def test_var_calculation(self):
        """VaR should be positive for a portfolio with return history."""
        monitor = RealtimeRiskMonitor({"var_confidence": 0.95})

        # Feed 50 portfolio snapshots with small random returns
        rng = np.random.RandomState(42)
        value = 100_000.0
        for _ in range(50):
            ret = rng.randn() * 0.01
            value *= 1 + ret
            snapshot = _make_portfolio(total_value=value)
            monitor.update(snapshot)

        metrics = monitor.get_metrics(_make_portfolio(total_value=value))
        assert metrics["var_95"] >= 0
        assert metrics["daily_vol"] > 0

    def test_drawdown_tracking(self):
        """Drawdown should be computed correctly from peak to current."""
        monitor = RealtimeRiskMonitor({"max_drawdown_pct": 0.15})

        # Push to a peak
        monitor.update(_make_portfolio(total_value=100_000.0))
        monitor.update(_make_portfolio(total_value=120_000.0))

        # Drop from peak
        alerts = monitor.update(_make_portfolio(total_value=96_000.0))

        metrics = monitor.get_metrics(_make_portfolio(total_value=96_000.0))
        expected_dd = (120_000.0 - 96_000.0) / 120_000.0
        assert abs(metrics["drawdown_pct"] - expected_dd) < 1e-6

        # Should have triggered a drawdown warning (20% > 15% threshold)
        assert any(a["type"] == "drawdown_warning" for a in alerts)
