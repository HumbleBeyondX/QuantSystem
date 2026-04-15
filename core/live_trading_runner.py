"""Live Trading Runner — bootstraps and executes live strategy trading.

Wires all system components into a production-grade live trading pipeline:
  Exchange (CCXT) → Market Data → Strategy → Risk Check → Order Execution

Safety layers (all active):
  1. Pre-trade risk checks (order size, concentration, leverage, daily limits)
  2. Real-time portfolio monitoring (drawdown, VaR, volatility)
  3. Circuit breaker (3-level: daily loss, max drawdown, zero value)
  4. Session loss limit (hard dollar cap per session)
  5. Exchange withdrawal permanently blocked
  6. Position reconciliation (broker vs internal)
  7. Broker health monitoring (connectivity checks)

Usage:
    runner = LiveTradingRunner("config/live/btc_sniper_live.yaml")
    await runner.setup()
    await runner.run()   # blocks until shutdown
"""

from __future__ import annotations

import asyncio
import os
import signal as os_signal
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import yaml

from core.enums import AssetClass, OrderSide, OrderType, SignalDirection
from core.events import EventBus, EventType, FillEvent, RiskAlertEvent
from core.logging import get_logger
from core.market_data_provider import CCXTBarProvider
from core.models import Bar, Fill, Order, PortfolioSnapshot
from execution.engine.ccxt_engine import CCXTExecutionEngine
from execution.oms.fill_tracker import FillTracker
from execution.oms.order_router import OrderRouter
from portfolio.position.position_manager import PositionManager
from portfolio.risk.risk_manager import RiskManager
from strategies.base import StrategyConfig
from strategies.registry import create_strategy, discover_strategies

logger = get_logger(__name__)


class LiveTradingRunner:
    """End-to-end live trading runner for a single strategy.

    Lifecycle:
        1. setup()   — Load config, connect exchange, initialize components
        2. warmup()  — Fetch historical bars, warm up strategy indicators
        3. run()     — Main loop: poll bars → signals → risk → orders → fills
        4. shutdown() — Graceful stop, optional position close, disconnect
    """

    def __init__(self, config_path: str) -> None:
        self._config_path = config_path
        self._config: Dict[str, Any] = {}
        self._running = False
        self._emergency_stopped = False

        # Components (initialized in setup())
        self._event_bus: Optional[EventBus] = None
        self._engine: Optional[CCXTExecutionEngine] = None
        self._order_router: Optional[OrderRouter] = None
        self._position_manager: Optional[PositionManager] = None
        self._risk_manager: Optional[RiskManager] = None
        self._bar_provider: Optional[CCXTBarProvider] = None
        self._strategy: Any = None

        # Session tracking
        self._session_start: Optional[datetime] = None
        self._session_pnl: float = 0.0
        self._session_trades: int = 0
        self._session_orders: int = 0
        self._last_reconcile_time: float = 0.0
        self._last_health_check: float = 0.0
        self._last_snapshot_time: float = 0.0
        self._exchange_snapshot: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def load_config(self) -> Dict[str, Any]:
        """Load and validate YAML configuration."""
        path = Path(self._config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config not found: {self._config_path}")

        with open(path, "r") as f:
            self._config = yaml.safe_load(f)

        logger.info(
            "config_loaded",
            path=str(path),
            strategy=self._config.get("strategy_id"),
            exchange=self._config.get("exchange", {}).get("id"),
            sandbox=self._config.get("exchange", {}).get("sandbox"),
        )
        return self._config

    async def setup(self) -> None:
        """Initialize all components and connect to exchange."""
        if not self._config:
            self.load_config()

        # Discover all strategies (triggers @register_strategy decorators)
        discover_strategies()

        cfg = self._config
        exchange_cfg = cfg.get("exchange", {})
        params = cfg.get("parameters", {})
        risk_cfg = cfg.get("risk", {})
        data_cfg = cfg.get("data", {})

        # ── 1. Event Bus ──
        self._event_bus = EventBus()

        # ── 2. Risk Manager (conservative live limits) ──
        self._risk_manager = RiskManager({
            "pre_trade": {
                "max_order_value": risk_cfg.get("max_order_value", 5000),
                "max_position_pct": risk_cfg.get("max_position_pct", 0.50),
                "max_leverage": risk_cfg.get("max_leverage", 15.0),
                "max_daily_orders": risk_cfg.get("max_daily_orders", 20),
                "max_daily_loss_pct": risk_cfg.get("max_daily_loss_pct", 0.03),
            },
            "circuit_breaker": {
                "daily_loss_limit_pct": risk_cfg.get("max_daily_loss_pct", 0.03),
                "max_drawdown_pct": risk_cfg.get("max_drawdown_pct", 0.10),
            },
        })

        # ── 3. Position Manager ──
        initial_capital = params.get("initial_capital", 500)
        self._position_manager = PositionManager(initial_cash=initial_capital)

        # ── 4. CCXT Execution Engine ──
        api_key = os.environ.get(f"{exchange_cfg['id'].upper()}_API_KEY", "")
        api_secret = os.environ.get(f"{exchange_cfg['id'].upper()}_API_SECRET", "")
        passphrase = os.environ.get(f"{exchange_cfg['id'].upper()}_PASSPHRASE", "")

        self._engine = CCXTExecutionEngine(
            exchange_id=exchange_cfg.get("id", "okx"),
            api_key=api_key,
            api_secret=api_secret,
            passphrase=passphrase,
            sandbox=exchange_cfg.get("sandbox", True),
            default_leverage=exchange_cfg.get("default_leverage", 10),
            default_margin_type=exchange_cfg.get("margin_type", "cross"),
        )

        logger.info(
            "connecting_exchange",
            exchange=exchange_cfg.get("id"),
            sandbox=exchange_cfg.get("sandbox", True),
        )
        await self._engine.connect()

        # Verify balance
        balance = await self._engine.get_balances()
        logger.info(
            "exchange_balance",
            total=balance.get("total", 0),
            free=balance.get("free", 0),
            used=balance.get("used", 0),
            currency=balance.get("currency", "USDT"),
        )

        live_equity = balance.get("total", 0)
        if live_equity > 0:
            self._position_manager = PositionManager(initial_cash=live_equity)

        # ── 5. Order Router ──
        self._order_router = OrderRouter()
        self._order_router.register_engine(
            AssetClass.CRYPTO_PERP,
            self._engine,
            name=exchange_cfg.get("id", "okx"),
        )
        self._order_router.register_engine(
            AssetClass.CRYPTO,
            self._engine,
            name=exchange_cfg.get("id", "okx"),
        )

        # ── 6. Market Data Provider ──
        symbol = cfg.get("symbols", ["BTC/USDT:USDT"])[0]
        timeframe = data_cfg.get("timeframe", "4h")
        self._bar_provider = CCXTBarProvider(
            exchange=self._engine._exchange,  # Share the CCXT instance
            symbol=symbol,
            timeframe=timeframe,
        )

        # ── 7. Strategy ──
        strategy_id = cfg.get("strategy_id", "btc_sniper")
        strategy_cfg = StrategyConfig(
            strategy_id=strategy_id,
            name=cfg.get("name", strategy_id),
            asset_class=AssetClass.CRYPTO,
            symbols=[symbol],
            parameters=params,
            mode="live",
        )

        self._strategy = create_strategy(strategy_id, strategy_cfg)

        if hasattr(self._strategy, "set_live_equity") and live_equity > 0:
            self._strategy.set_live_equity(live_equity)

        # ── 8. Start WebSocket monitor for async fills ──
        await self._engine.start_ws_monitor(symbols=[symbol])

        logger.info(
            "setup_complete",
            strategy=strategy_id,
            exchange=exchange_cfg.get("id"),
            sandbox=exchange_cfg.get("sandbox"),
            symbol=symbol,
            timeframe=timeframe,
            leverage=params.get("leverage", 10),
            risk_per_trade=params.get("risk_per_trade", 0.02),
            live_equity=live_equity,
        )

    # ------------------------------------------------------------------
    # Warmup
    # ------------------------------------------------------------------

    async def warmup(self) -> None:
        """Fetch historical bars and warm up strategy indicators."""
        data_cfg = self._config.get("data", {})
        warmup_count = data_cfg.get("warmup_bars", 200)

        bars = await self._bar_provider.fetch_warmup_bars(warmup_count)

        logger.info("warming_up_strategy", bars=len(bars))
        for bar in bars:
            self._strategy.on_bar(bar.symbol, bar)

        # Initialize strategy
        if hasattr(self._strategy, "initialize"):
            self._strategy.initialize()

        logger.info(
            "strategy_warmed_up",
            bars_fed=len(bars),
            strategy=self._config.get("strategy_id"),
        )

    # ------------------------------------------------------------------
    # Exchange Position Sync (prevents duplicate orders on restart)
    # ------------------------------------------------------------------

    async def sync_exchange_positions(self) -> None:
        """Sync existing exchange positions into strategy & position manager.

        CRITICAL: Must be called after warmup() and before run().
        Without this, every restart would treat the strategy as flat and
        open a duplicate position — the root cause of the 3x position bug.

        Uses retry logic because CCXT may need time to load position data
        after initial connection.
        """
        exchange_positions: Dict[str, float] = {}

        # Retry up to 3 times with increasing delay (CCXT startup timing)
        for attempt in range(3):
            try:
                exchange_positions = await self._engine.get_positions()
                if exchange_positions:
                    break
                # Empty result — wait and retry
                if attempt < 2:
                    wait = (attempt + 1) * 2
                    logger.info(
                        "sync_positions_retry",
                        attempt=attempt + 1,
                        wait_seconds=wait,
                        reason="empty_result",
                    )
                    await asyncio.sleep(wait)
            except Exception as e:
                logger.warning("sync_positions_attempt_failed", attempt=attempt + 1, error=str(e))
                if attempt < 2:
                    await asyncio.sleep(2)

        synced = 0
        for sym, qty in exchange_positions.items():
            if abs(qty) < 1e-12:
                continue

            # ── Inject into strategy's internal position tracker ──
            old_pos = self._strategy.get_position(sym)
            if abs(old_pos) < 1e-12 or abs(old_pos - qty) > 1e-6:
                self._strategy._positions[sym] = qty
                synced += 1
                logger.info(
                    "POSITION_SYNCED_TO_STRATEGY",
                    symbol=sym,
                    exchange_qty=qty,
                    direction="LONG" if qty > 0 else "SHORT",
                    was=old_pos,
                )

        # Also fetch detailed positions for accurate session tracking
        if synced > 0:
            try:
                detailed = await self._engine.get_positions_detailed()
                for pos in detailed:
                    upnl = pos.get("unrealized_pnl", 0)
                    self._session_pnl = upnl  # Initialize with current unrealized
                    self._session_trades = 1   # We inherited an existing trade
                    self._session_orders = 0   # No new orders yet this session
            except Exception:
                pass

            logger.info(
                "exchange_position_sync_complete",
                synced_count=synced,
                positions={
                    s: q for s, q in exchange_positions.items() if abs(q) > 1e-12
                },
            )
        else:
            logger.info("exchange_position_sync_clean", message="No existing positions on exchange")

    # ------------------------------------------------------------------
    # Main Loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main live trading loop. Blocks until shutdown."""
        self._running = True
        self._session_start = datetime.now(timezone.utc)
        self._emergency_stopped = False

        data_cfg = self._config.get("data", {})
        poll_interval = data_cfg.get("poll_interval_seconds", 30)
        risk_cfg = self._config.get("risk", {})
        max_session_loss = risk_cfg.get("max_session_loss_usd", 50)
        reconcile_interval = self._config.get("reconciliation", {}).get("interval_seconds", 60)

        logger.info(
            "live_trading_started",
            strategy=self._config.get("strategy_id"),
            poll_interval=poll_interval,
            max_session_loss=max_session_loss,
        )

        loop_count = 0
        while self._running and not self._emergency_stopped:
            try:
                loop_count += 1
                now = time.time()

                # ── Phase 1: Check for new completed bar ──
                bar = await self._bar_provider.poll_new_bar()
                if bar:
                    await self._process_new_bar(bar)

                # ── Phase 2: Process fills from engine ──
                await self._process_fills()

                # ── Phase 3: Position reconciliation (periodic) ──
                if now - self._last_reconcile_time > reconcile_interval:
                    await self._reconcile_positions()
                    self._last_reconcile_time = now

                # ── Phase 4: Exchange snapshot for dashboard (every 15s) ──
                if now - self._last_snapshot_time > 15:
                    await self._update_exchange_snapshot()
                    self._last_snapshot_time = now

                # ── Phase 5: Broker health check (every 2 minutes) ──
                if now - self._last_health_check > 120:
                    await self._health_check()
                    self._last_health_check = now

                # ── Phase 5: Session loss limit check ──
                if abs(self._session_pnl) > max_session_loss and self._session_pnl < 0:
                    logger.critical(
                        "SESSION_LOSS_LIMIT_HIT",
                        session_pnl=self._session_pnl,
                        limit=max_session_loss,
                    )
                    await self._emergency_stop("Session loss limit reached")
                    break

                # ── Phase 6: Risk monitoring ──
                snapshot = self._position_manager.get_snapshot()
                alerts = self._risk_manager.update(snapshot)
                for alert in alerts:
                    logger.warning("risk_alert", **alert)
                    if alert.get("triggered"):
                        await self._emergency_stop(f"Circuit breaker: {alert.get('level', 'unknown')}")
                        break

                if self._emergency_stopped:
                    break

                await asyncio.sleep(poll_interval)

            except asyncio.CancelledError:
                logger.info("trading_loop_cancelled")
                break
            except Exception as e:
                logger.error(
                    "trading_loop_error",
                    error=str(e),
                    traceback=traceback.format_exc(),
                )
                # Don't crash on transient errors — continue after delay
                await asyncio.sleep(5)

        logger.info(
            "live_trading_stopped",
            session_pnl=self._session_pnl,
            session_trades=self._session_trades,
            loops=loop_count,
        )

    # ------------------------------------------------------------------
    # Order sizing & exchange precision
    # ------------------------------------------------------------------

    def _adjust_order_size(
        self,
        symbol: str,
        raw_qty: float,
        is_entry: bool,
    ) -> tuple:
        """Adjust order quantity to meet exchange minimums and precision.

        For ENTRY orders, applies the following logic:
          1. Fetch exchange minimum order size (e.g. OKX BTC min = 0.01)
          2. If raw_qty < min_amount AND force_min_lot is enabled:
             - Round up to min_amount
             - Calculate new effective risk = min_qty * stop_distance
             - If margin required > equity * max_margin_pct → REJECT
             - Else: proceed with min_qty and log the risk adjustment
          3. Apply exchange amount precision rounding
          4. Final validation: ensure qty >= min_amount after rounding

        For EXIT orders:
          - Just apply precision rounding (exchange allows closing full position)

        Returns:
            (adjusted_qty, metadata_dict)  — qty=0 means SKIP this order
        """
        params = self._config.get("parameters", {})
        force_min_lot = params.get("force_min_lot", True)
        max_risk_override_pct = params.get("max_risk_override_pct", 0.50)
        leverage = params.get("leverage", 10)

        meta = {}
        qty = abs(raw_qty)

        # Get exchange market info
        market_info = self._engine.get_market_info(symbol)
        min_amount = market_info.get("min_amount", 0)
        min_cost = market_info.get("min_cost", 0)
        contract_size = market_info.get("contract_size", 0)

        # For perpetual contracts, the effective minimum is at least 1 contract.
        # CCXT's min_amount can be misleading for swaps — it may report the
        # precision step rather than the true minimum order size.
        # E.g., OKX ETH-USDT-SWAP: contract_size=0.1 ETH, minSz=1 contract
        #   → real minimum = 0.1 ETH, but CCXT may report min_amount=0.01
        if contract_size > 0:
            min_amount = max(min_amount, contract_size)

        if min_amount <= 0:
            # No minimum known — just round to precision
            qty = self._engine.amount_to_precision(symbol, qty)
            return qty, meta

        if not is_entry:
            # For exits, round to precision but don't enforce minimum
            # (exchange typically allows closing any position)
            qty = self._engine.amount_to_precision(symbol, qty)
            if qty <= 0:
                qty = min_amount  # Edge case: round-down to 0
            return qty, meta

        # ── Entry order: check against minimum ──
        if qty >= min_amount:
            # Already above minimum — just round to precision
            qty = self._engine.amount_to_precision(symbol, qty)
            if qty < min_amount:
                qty = min_amount  # Precision rounding caused drop below min
            return qty, meta

        # qty < min_amount — this is the key scenario for small accounts
        if not force_min_lot:
            logger.warning(
                "ORDER_BELOW_MINIMUM",
                symbol=symbol,
                calculated_qty=round(qty, 8),
                min_amount=min_amount,
                action="SKIP (force_min_lot=false)",
            )
            return 0.0, {"skipped": True, "reason": "below_exchange_minimum"}

        # Force round up to minimum order size
        old_qty = qty
        qty = min_amount

        # Calculate the implications
        try:
            balance = self._position_manager.get_snapshot()
            equity = getattr(balance, "cash", 0) or params.get("initial_capital", 90)
        except Exception:
            equity = params.get("initial_capital", 90)

        # Use live equity if available
        if hasattr(self._strategy, "_live_equity") and self._strategy._live_equity:
            equity = self._strategy._live_equity

        # Get current price from last bar
        bars = getattr(self._strategy, "_bars", {})
        symbol_bars = bars.get(symbol)
        price = symbol_bars[-1].close if symbol_bars and len(symbol_bars) > 0 else 0
        if price <= 0:
            logger.error("cannot_determine_price", symbol=symbol)
            return 0.0, {"skipped": True, "reason": "no_price_data"}

        # Margin check: can we afford the minimum position?
        notional = qty * price
        margin_required = notional / leverage
        max_margin = equity * params.get("max_margin_pct", 0.30)

        if margin_required > equity * 0.90:
            # Would use >90% of capital as margin — too dangerous
            logger.warning(
                "MIN_LOT_MARGIN_EXCEEDED",
                symbol=symbol,
                min_qty=min_amount,
                notional=round(notional, 2),
                margin_required=round(margin_required, 2),
                equity=round(equity, 2),
                action="SKIP",
            )
            return 0.0, {"skipped": True, "reason": "margin_exceeds_capital"}

        # Risk assessment: what is the actual risk with min lot?
        stop_dist = getattr(self._strategy, "_stop_dist", {}).get(symbol, 0)
        if stop_dist > 0:
            actual_risk = qty * stop_dist
            actual_risk_pct = actual_risk / equity if equity > 0 else 999
        else:
            actual_risk = 0
            actual_risk_pct = 0

        original_risk = old_qty * stop_dist if stop_dist > 0 else 0
        risk_multiplier = qty / old_qty if old_qty > 0 else 1

        meta.update({
            "force_min_lot": True,
            "original_qty": round(old_qty, 8),
            "adjusted_qty": round(qty, 8),
            "min_amount": min_amount,
            "risk_multiplier": round(risk_multiplier, 1),
            "actual_risk_usd": round(actual_risk, 2),
            "actual_risk_pct": round(actual_risk_pct, 4),
            "margin_required": round(margin_required, 2),
            "margin_pct": round(margin_required / equity, 4) if equity > 0 else 0,
        })

        if actual_risk_pct > max_risk_override_pct:
            logger.warning(
                "MIN_LOT_RISK_TOO_HIGH",
                symbol=symbol,
                actual_risk_pct=f"{actual_risk_pct:.1%}",
                max_allowed=f"{max_risk_override_pct:.1%}",
                actual_risk_usd=round(actual_risk, 2),
                action="SKIP",
            )
            return 0.0, {"skipped": True, "reason": "risk_override_exceeded"}

        # Apply precision rounding
        qty = self._engine.amount_to_precision(symbol, qty)
        if qty < min_amount:
            qty = min_amount

        logger.warning(
            "MIN_LOT_ADJUSTMENT",
            symbol=symbol,
            original_qty=round(old_qty, 8),
            adjusted_qty=qty,
            risk_multiplier=f"{risk_multiplier:.1f}x",
            actual_risk=f"${actual_risk:.2f} ({actual_risk_pct:.1%} of equity)",
            margin=f"${margin_required:.2f} ({margin_required/equity:.1%} of equity)" if equity > 0 else "N/A",
        )

        return qty, meta

    async def _process_new_bar(self, bar: Bar) -> None:
        """Feed new bar to strategy and process any signals."""
        symbol = bar.symbol
        strategy = self._strategy

        # Update live equity from exchange
        try:
            balance = await self._engine.get_balances()
            live_equity = balance.get("total", 0)
            if hasattr(strategy, "set_live_equity") and live_equity > 0:
                strategy.set_live_equity(live_equity)
        except Exception as e:
            logger.warning("balance_fetch_failed", error=str(e))

        # Feed bar to strategy
        strategy.on_bar(symbol, bar)

        # Generate target positions (internally calls generate_signals + sizing)
        targets = strategy.calculate_target_positions()
        if not targets:
            return

        logger.info(
            "targets_generated",
            count=len(targets),
            targets=[
                {"symbol": t.symbol, "qty": round(t.target_quantity, 6), "urgency": t.urgency}
                for t in targets
            ],
        )

        # Convert targets to orders
        for target in targets:
            qty = target.target_quantity
            current_pos = strategy.get_position(target.symbol)

            # Determine if this is an entry (new position) or exit (close position)
            if abs(qty) < 1e-12:
                # FLAT signal → close existing position
                if abs(current_pos) < 1e-12:
                    continue  # Already flat

                # For exits, get the actual position from exchange to be safe
                exit_qty = abs(current_pos)
                exit_qty, exit_meta = self._adjust_order_size(
                    target.symbol, exit_qty, is_entry=False,
                )

                order = Order(
                    order_id=uuid4(),
                    strategy_id=strategy.strategy_id,
                    symbol=target.symbol,
                    side=OrderSide.SELL if current_pos > 0 else OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quantity=exit_qty,
                    asset_class=AssetClass.CRYPTO_PERP,
                    metadata={
                        "reduce_only": True,
                        "exchange": self._config.get("exchange", {}).get("id"),
                        "reason": "signal_flat",
                        **exit_meta,
                    },
                )
            else:
                # Entry signal
                if abs(current_pos) > 1e-12:
                    logger.warning("already_in_position", symbol=target.symbol, pos=current_pos)
                    continue  # Don't double up

                # ── Apply min-lot adjustment and precision ──
                adjusted_qty, size_meta = self._adjust_order_size(
                    target.symbol, abs(qty), is_entry=True,
                )

                if adjusted_qty <= 0:
                    logger.warning(
                        "ORDER_SKIPPED",
                        symbol=target.symbol,
                        raw_qty=round(abs(qty), 8),
                        reason=size_meta.get("reason", "adjusted_to_zero"),
                    )
                    continue

                order = Order(
                    order_id=uuid4(),
                    strategy_id=strategy.strategy_id,
                    symbol=target.symbol,
                    side=OrderSide.BUY if qty > 0 else OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    quantity=adjusted_qty,
                    asset_class=AssetClass.CRYPTO_PERP,
                    metadata={
                        "leverage": int(self._config.get("parameters", {}).get("leverage", 10)),
                        "exchange": self._config.get("exchange", {}).get("id"),
                        "reason": "signal_entry",
                        **size_meta,
                    },
                )

            # Pre-trade risk check
            snapshot = self._position_manager.get_snapshot()
            approved, reason = self._risk_manager.check_order(order, snapshot)
            if not approved:
                logger.warning(
                    "order_rejected_risk",
                    symbol=order.symbol,
                    side=order.side.value,
                    qty=order.quantity,
                    reason=reason,
                )
                continue

            # Route order to exchange
            try:
                broker_id = await self._order_router.route(order)
                self._session_orders += 1
                logger.info(
                    "ORDER_SUBMITTED",
                    symbol=order.symbol,
                    side=order.side.value,
                    qty=round(order.quantity, 6),
                    broker_id=broker_id,
                    metadata=order.metadata,
                )
            except Exception as e:
                logger.error(
                    "order_submission_failed",
                    symbol=order.symbol,
                    error=str(e),
                )

    async def _process_fills(self) -> None:
        """Poll fills from execution engine and update positions."""
        try:
            fills = await self._engine.get_fills()
        except Exception as e:
            logger.warning("fill_poll_error", error=str(e))
            return

        for fill in fills:
            # Update position manager
            self._position_manager.on_fill(fill)

            # Forward fill to strategy (updates internal position tracking)
            if hasattr(self._strategy, "on_fill"):
                self._strategy.on_fill(fill)

            # Track session stats
            self._session_trades += 1

            # Publish fill event
            await self._event_bus.publish_nowait(FillEvent(
                fill_id=fill.fill_id,
                order_id=fill.order_id,
                strategy_id=fill.strategy_id,
                symbol=fill.symbol,
                side=fill.side,
                price=fill.price,
                quantity=fill.quantity,
                commission=fill.commission,
            ))

            logger.info(
                "FILL_PROCESSED",
                symbol=fill.symbol,
                side=fill.side.value,
                price=fill.price,
                qty=round(fill.quantity, 6),
                commission=fill.commission,
            )

    async def _reconcile_positions(self) -> None:
        """Compare internal positions with exchange positions."""
        try:
            exchange_positions = await self._engine.get_positions()
            internal_positions = {}

            for pos in self._position_manager.get_all_positions():
                internal_positions[pos.symbol] = (
                    pos.quantity if pos.side.value == "LONG" else -pos.quantity
                )

            # Check for mismatches
            tolerance = self._config.get("reconciliation", {}).get("tolerance", 0.0001)
            all_symbols = set(list(exchange_positions.keys()) + list(internal_positions.keys()))

            for sym in all_symbols:
                exchange_qty = exchange_positions.get(sym, 0.0)
                internal_qty = internal_positions.get(sym, 0.0)
                diff = abs(exchange_qty - internal_qty)
                if diff > tolerance:
                    logger.warning(
                        "POSITION_MISMATCH",
                        symbol=sym,
                        exchange_qty=exchange_qty,
                        internal_qty=internal_qty,
                        diff=diff,
                    )

        except Exception as e:
            logger.warning("reconciliation_error", error=str(e))

    async def _health_check(self) -> None:
        """Check exchange connectivity."""
        try:
            healthy = await self._engine.health_check()
            if not healthy:
                logger.error("EXCHANGE_UNHEALTHY", exchange=self._engine.exchange_id)
                await self._event_bus.publish_nowait(RiskAlertEvent(
                    alert_level="CRITICAL",
                    message=f"Exchange {self._engine.exchange_id} health check failed",
                    source="live_runner",
                ))
        except Exception as e:
            logger.error("health_check_error", error=str(e))

    # ------------------------------------------------------------------
    # Emergency Controls
    # ------------------------------------------------------------------

    async def _emergency_stop(self, reason: str) -> None:
        """Trigger emergency stop: close all positions and halt trading."""
        self._emergency_stopped = True
        self._running = False

        logger.critical("EMERGENCY_STOP", reason=reason)

        # Close all positions
        try:
            positions = await self._engine.get_positions()
            for symbol, qty in positions.items():
                if abs(qty) < 1e-10:
                    continue
                close_order = Order(
                    order_id=uuid4(),
                    strategy_id=self._config.get("strategy_id", ""),
                    symbol=symbol,
                    side=OrderSide.SELL if qty > 0 else OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quantity=abs(qty),
                    asset_class=AssetClass.CRYPTO_PERP,
                    metadata={"reduce_only": True, "reason": f"emergency: {reason}"},
                )
                try:
                    broker_id = await self._order_router.route(close_order)
                    logger.info("emergency_close_sent", symbol=symbol, qty=qty, broker_id=broker_id)
                except Exception as e:
                    logger.error("emergency_close_failed", symbol=symbol, error=str(e))
        except Exception as e:
            logger.error("emergency_position_fetch_failed", error=str(e))

        # Publish risk alert
        await self._event_bus.publish_nowait(RiskAlertEvent(
            alert_level="KILL",
            message=f"Emergency stop: {reason}",
            source="live_runner",
        ))

    async def force_close_all(self) -> Dict[str, Any]:
        """Manually close all positions (called from API)."""
        results = {}
        try:
            positions = await self._engine.get_positions()
            for symbol, qty in positions.items():
                if abs(qty) < 1e-10:
                    continue
                close_order = Order(
                    order_id=uuid4(),
                    strategy_id=self._config.get("strategy_id", ""),
                    symbol=symbol,
                    side=OrderSide.SELL if qty > 0 else OrderSide.BUY,
                    order_type=OrderType.MARKET,
                    quantity=abs(qty),
                    asset_class=AssetClass.CRYPTO_PERP,
                    metadata={"reduce_only": True, "reason": "manual_close"},
                )
                try:
                    broker_id = await self._order_router.route(close_order)
                    results[symbol] = {"status": "closed", "qty": qty, "broker_id": broker_id}
                except Exception as e:
                    results[symbol] = {"status": "error", "error": str(e)}
        except Exception as e:
            results["error"] = str(e)
        return results

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def shutdown(self, close_positions: bool = False) -> None:
        """Graceful shutdown."""
        self._running = False

        logger.info("shutdown_initiated", close_positions=close_positions)

        if close_positions:
            await self.force_close_all()

        # Disconnect from exchange
        if self._engine:
            try:
                await self._engine.disconnect()
            except Exception as e:
                logger.warning("engine_disconnect_error", error=str(e))

        # Final stats
        duration = (
            (datetime.now(timezone.utc) - self._session_start).total_seconds()
            if self._session_start else 0
        )
        logger.info(
            "shutdown_complete",
            session_pnl=self._session_pnl,
            session_trades=self._session_trades,
            session_orders=self._session_orders,
            duration_seconds=round(duration),
        )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Get current runner status (for API/dashboard).

        Combines internal state with cached exchange data for accurate display.
        Exchange data is updated by _update_exchange_snapshot() during the main loop.
        """
        snapshot = self._exchange_snapshot  # Set by periodic updates

        return {
            "running": self._running,
            "emergency_stopped": self._emergency_stopped,
            "strategy_id": self._config.get("strategy_id"),
            "exchange": self._config.get("exchange", {}).get("id"),
            "sandbox": self._config.get("exchange", {}).get("sandbox"),
            "symbol": self._config.get("symbols", [""])[0],
            "session_pnl": self._session_pnl,
            "session_trades": self._session_trades,
            "session_orders": self._session_orders,
            "session_start": self._session_start.isoformat() if self._session_start else None,
            "position_manager_snapshot": snapshot,
        }

    async def _update_exchange_snapshot(self) -> None:
        """Fetch real positions + balance from exchange for status display."""
        try:
            # Get balance
            balance = await self._engine.get_balances()
            equity = balance.get("total", 0)
            cash = balance.get("free", 0)

            # Get detailed positions (with P&L, entry price, etc.)
            detailed = await self._engine.get_positions_detailed()
            total_upnl = sum(p.get("unrealized_pnl", 0) for p in detailed)

            positions = []
            for pos in detailed:
                qty = pos.get("contracts", 0)
                if pos.get("side") == "short":
                    qty = -qty
                positions.append({
                    "symbol": pos.get("symbol", ""),
                    "quantity": qty,
                    "avg_price": pos.get("entry_price", 0),
                    "market_value": pos.get("notional", 0),
                    "unrealized_pnl": pos.get("unrealized_pnl", 0),
                    "mark_price": pos.get("mark_price", 0),
                    "leverage": pos.get("leverage", 0),
                    "liquidation_price": pos.get("liquidation_price", 0),
                })

            self._exchange_snapshot = {
                "total_value": equity,
                "cash": cash,
                "unrealized_pnl": total_upnl,
                "realized_pnl": self._session_pnl - total_upnl,
                "daily_pnl": equity - self._position_manager._initial_cash if self._position_manager else 0,
                "num_positions": len(positions),
                "positions": positions,
            }

            # Keep session P&L in sync with exchange unrealized
            if len(detailed) > 0:
                self._session_pnl = total_upnl

        except Exception as e:
            logger.debug("exchange_snapshot_update_failed", error=str(e))
