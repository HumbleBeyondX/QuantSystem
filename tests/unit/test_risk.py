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


# ---------------------------------------------------------------------------
# Exchange-truth risk snapshot (regression for 2026-06-07 false circuit break)
# ---------------------------------------------------------------------------


class TestExchangeTruthRiskSnapshot:
    """Regression tests for the 2026-06-07 incident: a PROFITABLE perp short
    triggered "Drawdown 75.5%" and emergency-stopped the live trader.

    Root cause: PositionManager uses spot-style cash accounting. A perp SELL
    credits the full notional as cash ($86.98 → $277.57), and get_snapshot()
    then ALSO adds the position's market value (+$190.59) → fake total_value
    $468.15. When the short closed, equity "collapsed" to $114.70 and the
    circuit breaker read a 75.5% drawdown on an account that actually GAINED
    money ($86.98 → $89.74 on exchange).

    Fix: LiveTradingRunner._build_risk_snapshot() overlays exchange-reported
    equity onto the snapshot used for ALL risk checks.
    """

    # Exact figures from the incident logs
    INITIAL_CASH = 86.98
    ENTRY_PRICE = 1905.89
    EXIT_PRICE = 1628.51
    QTY = 0.1

    def _runner_with_state(self, exchange_equity: float | None):
        """Build a bare runner (no exchange connection) with a PositionManager
        that has replayed the incident's SELL fill, and optionally an
        exchange snapshot reporting true equity."""
        from core.enums import OrderSide
        from core.live_trading_runner import LiveTradingRunner
        from core.models import Fill
        from portfolio.position.position_manager import PositionManager

        runner = LiveTradingRunner.__new__(LiveTradingRunner)  # skip __init__ I/O
        runner._position_manager = PositionManager(initial_cash=self.INITIAL_CASH)
        runner._exchange_snapshot = None
        runner._exchange_snapshot_time = 0.0

        # Replay the entry SELL fill (opens the short, corrupts spot-cash)
        fill = Fill(
            fill_id=str(uuid4()),
            order_id=str(uuid4()),
            strategy_id="eth_sniper",
            symbol="ETH/USDT:USDT",
            side=OrderSide.SELL,
            quantity=self.QTY,
            price=self.ENTRY_PRICE,
            commission=0.0095,
            timestamp=datetime.utcnow(),
            asset_class=AssetClass.CRYPTO,
        )
        runner._position_manager.on_fill(fill)

        if exchange_equity is not None:
            runner._exchange_snapshot = {
                "total_value": exchange_equity,
                "cash": exchange_equity,
                "unrealized_pnl": 0.0,
                "daily_pnl": exchange_equity - self.INITIAL_CASH,
                "num_positions": 1,
                "positions": [],
            }
        return runner

    def test_perp_short_entry_keeps_internal_equity_true(self):
        """ROOT FIX (audit P1-6): opening a perp short must NOT move cash by
        notional. Internal equity stays ≈ initial minus commission — the old
        spot-style accounting inflated it to ~$468."""
        runner = self._runner_with_state(exchange_equity=None)
        internal = runner._position_manager.get_snapshot()
        assert abs(internal.total_value - (self.INITIAL_CASH - 0.0095)) < 0.01, (
            f"perp short entry distorted equity: {internal.total_value:.2f}"
        )

    def test_risk_snapshot_uses_exchange_equity(self):
        """With exchange data present, risk snapshot must report TRUE equity."""
        runner = self._runner_with_state(exchange_equity=86.98)
        snap = runner._build_risk_snapshot()
        assert abs(snap.total_value - 86.98) < 0.01
        assert snap.positions  # internal positions still attached for checks

    def test_risk_snapshot_falls_back_to_internal_without_exchange(self):
        """No exchange snapshot yet (first seconds) → internal fallback."""
        runner = self._runner_with_state(exchange_equity=None)
        snap = runner._build_risk_snapshot()
        internal = runner._position_manager.get_snapshot()
        assert abs(snap.total_value - internal.total_value) < 0.01

    def _exit_fill(self):
        from core.enums import OrderSide
        from core.models import Fill
        return Fill(
            fill_id=str(uuid4()),
            order_id=str(uuid4()),
            strategy_id="eth_sniper",
            symbol="ETH/USDT:USDT",
            side=OrderSide.BUY,
            quantity=self.QTY,
            price=self.EXIT_PRICE,
            commission=0.0081,
            timestamp=datetime.utcnow(),
            asset_class=AssetClass.CRYPTO,
        )

    def test_profitable_short_does_not_trip_circuit_breaker(self):
        """End-to-end replay: open short → close with profit. With exchange-
        truth equity the breaker must stay quiet (real equity only ever rose).

        Uses the REAL CircuitBreaker config keys (portfolio_loss_limit_pct /
        max_drawdown_kill_pct) — the runner previously passed wrong key names
        and the breaker silently ran on defaults.
        """
        breaker = CircuitBreaker({"max_drawdown_kill_pct": 0.20,
                                  "portfolio_loss_limit_pct": 0.05})

        runner = self._runner_with_state(exchange_equity=86.98)

        # Short open, equity flat at ~87
        r1 = breaker.check(runner._build_risk_snapshot())
        assert not r1["triggered"]

        # Short closes with profit — exchange reports the TRUE equity path
        runner._exchange_snapshot["total_value"] = 89.74
        runner._exchange_snapshot["cash"] = 89.74
        r2 = breaker.check(runner._build_risk_snapshot())
        assert not r2["triggered"], (
            f"circuit breaker fired on a profitable trade: {r2.get('reason')}"
        )

    def test_full_6_07_replay_internal_path_stays_quiet(self):
        """Full incident replay on INTERNAL snapshots (no exchange overlay).

        With correct perp accounting the internal equity path is
        87 → 87 (short open) → ~114.7 (profitable close of a true 0.1 ETH
        short) — monotonically rising, so the breaker must stay quiet even
        without the exchange-truth overlay. This is the same sequence that
        produced the false "75.5% drawdown" kill on 2026-06-07.
        """
        breaker = CircuitBreaker({"max_drawdown_kill_pct": 0.20,
                                  "portfolio_loss_limit_pct": 0.99})

        # 1. Startup: clean equity → breaker captures initial_value = 86.98
        runner = self._runner_with_state(exchange_equity=None)
        # (fill already applied by helper — entry state)
        r1 = breaker.check(runner._position_manager.get_snapshot())
        assert not r1["triggered"]

        # 2. Exit BUY fill closes the short with profit
        runner._position_manager.on_fill(self._exit_fill())
        snap = runner._position_manager.get_snapshot()
        expected = (self.INITIAL_CASH - 0.0095
                    + (self.ENTRY_PRICE - self.EXIT_PRICE) * self.QTY - 0.0081)
        assert abs(snap.total_value - expected) < 0.02, (
            f"close accounting off: {snap.total_value:.2f} vs {expected:.2f}"
        )
        result = breaker.check(snap)
        assert not result["triggered"], (
            f"breaker fired on correct accounting: {result.get('reason')}"
        )

    def test_perp_flip_pnl_pro_rata(self):
        """Flip fills realize PnL only on the covering quantity (audit P1-12)."""
        from portfolio.position.position_manager import PositionManager
        from core.enums import OrderSide
        from core.models import Fill

        pm = PositionManager(initial_cash=1000.0)
        # Short 0.1 @ 1900
        pm.on_fill(Fill(
            fill_id=str(uuid4()), order_id=str(uuid4()), strategy_id="s",
            symbol="ETH/USDT:USDT", side=OrderSide.SELL, quantity=0.1,
            price=1900.0, commission=0.15, timestamp=datetime.utcnow(),
            asset_class=AssetClass.CRYPTO,
        ))
        # BUY 0.15 @ 1850 → covers 0.1 (pnl on 0.1 only), flips LONG 0.05
        pm.on_fill(Fill(
            fill_id=str(uuid4()), order_id=str(uuid4()), strategy_id="s",
            symbol="ETH/USDT:USDT", side=OrderSide.BUY, quantity=0.15,
            price=1850.0, commission=0.15, timestamp=datetime.utcnow(),
            asset_class=AssetClass.CRYPTO,
        ))
        pos = pm.get_all_positions()[0]
        assert pos.side == PositionSide.LONG
        assert abs(pos.quantity - 0.05) < 1e-9
        assert abs(pos.average_entry_price - 1850.0) < 1e-9
        # covering pnl = (1900-1850)*0.1 = 5.00, minus 0.1/0.15 of commission
        expected_pnl = 5.0 - 0.15 * (0.1 / 0.15)
        assert abs(pos.realized_pnl - expected_pnl) < 1e-6, (
            f"flip pnl {pos.realized_pnl:.4f} != expected {expected_pnl:.4f} "
            "(old bug: used full 0.15 qty → 7.35)"
        )

    def test_market_order_priced_via_ref_price(self):
        """Audit P0-2: MARKET orders (limit_price=None) must be valued with
        ref_price — a 218%-concentration entry on the real $87 account gets
        REJECTED instead of sailing through at $0 estimated value."""
        check = PreTradeRiskCheck({"max_order_value": 5000,
                                   "max_position_pct": 0.50,
                                   "max_daily_orders": 100})
        portfolio = _make_portfolio(total_value=87.0, cash=87.0)
        order = _make_order(symbol="ETH/USDT:USDT", side=OrderSide.BUY,
                            quantity=0.1, limit_price=None)  # market order

        # With ref_price: notional 0.1×1900 = $190 → 218% of equity → reject
        ok, reason = check.check(order, portfolio, ref_price=1900.0)
        assert not ok and "concentration" in reason.lower()

        # Without any price: cannot value a risk-increasing order → reject
        ok2, reason2 = check.check(order, portfolio)
        assert not ok2 and "value" in reason2.lower()

        # Small enough order passes (0.01 ETH ≈ $19 = 22% of equity)
        small = _make_order(symbol="ETH/USDT:USDT", side=OrderSide.BUY,
                            quantity=0.01, limit_price=None)
        ok3, _ = check.check(small, portfolio, ref_price=1900.0)
        assert ok3
