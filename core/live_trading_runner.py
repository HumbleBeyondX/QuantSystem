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

from core.enums import AssetClass, OrderSide, OrderType, PositionSide, SignalDirection
from core.events import EventBus, EventType, FillEvent, RiskAlertEvent
from core.logging import get_logger
from core.market_data_provider import CCXTBarProvider
from core.models import Bar, Fill, Order, PortfolioSnapshot, Position
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
        # Realized-PnL baseline (PositionManager total at session start).
        # Session loss = (realized since baseline) + (current unrealized).
        # The old code overwrote _session_pnl with unrealized-only every 15s,
        # erasing realized losses — the $15 session cap NEVER actually worked
        # across multiple trades (2026-06 audit P0-3).
        self._session_realized_baseline: float = 0.0
        self._last_reconcile_time: float = 0.0
        self._last_health_check: float = 0.0
        self._last_snapshot_time: float = 0.0
        self._exchange_snapshot: Optional[Dict[str, Any]] = None
        self._exchange_snapshot_time: float = 0.0
        self._mismatch_streak: Dict[str, int] = {}

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
            # NOTE: CircuitBreaker reads portfolio_loss_limit_pct /
            # max_drawdown_kill_pct — the previous keys (daily_loss_limit_pct /
            # max_drawdown_pct) were silently ignored and the breaker ran on
            # defaults (5% / 20%) regardless of the YAML risk settings.
            "circuit_breaker": {
                "portfolio_loss_limit_pct": risk_cfg.get("max_daily_loss_pct", 0.03),
                "max_drawdown_kill_pct": risk_cfg.get("max_drawdown_pct", 0.10),
            },
            # Realtime monitor warns BEFORE the breaker kills — its warning
            # threshold must sit below the kill threshold. Previously no
            # "realtime" config was passed at all (ran on defaults).
            "realtime": {
                "max_drawdown_pct": risk_cfg.get("max_drawdown_pct", 0.10) * 0.8,
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

        Syncs three state stores:
          1. ``strategy._positions``           — plain qty dict (drives
             entry logic so duplicate orders aren't opened)
          2. ``strategy`` extended state       — via ``restore_position_state``
             when the strategy supports it: entry_price, ATR, stop distance.
             Without this, exit signals use fallback values and the position
             may not trigger its actual stop-loss.
          3. ``position_manager._positions``   — Position object, so the
             reconciliation loop stops reporting POSITION_MISMATCH and the
             portfolio snapshot / risk checks see the open position.

        Uses retry logic because CCXT may need time to load position data
        after initial connection.
        """
        # Fetch detailed positions directly — they include entry_price,
        # mark_price, upnl which we need for state reconstruction.
        detailed: List[Dict[str, Any]] = []
        fetch_succeeded = False

        for attempt in range(3):
            try:
                detailed = await self._engine.get_positions_detailed()
                fetch_succeeded = True
                if detailed:
                    break
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

        # "Confirmed empty" and "could not fetch" are NOT the same thing.
        # If every attempt raised, a real position may exist that we cannot
        # see — trading blind would open a duplicate on the next signal.
        if not fetch_succeeded:
            logger.critical(
                "SYNC_POSITIONS_UNAVAILABLE_REFUSING_TO_TRADE",
                message="Exchange position API unreachable after 3 attempts; "
                        "cannot verify existing positions. Fix connectivity "
                        "and restart.",
            )
            raise RuntimeError(
                "Position sync failed: exchange API unreachable — refusing to "
                "start trading with unverified position state"
            )

        # Filter to non-zero positions
        open_positions = [p for p in detailed if abs(p.get("contracts", 0) or 0) > 1e-12]

        if not open_positions:
            logger.info("exchange_position_sync_clean",
                        message="No existing positions on exchange")
            return

        strategy_id = self._config.get("strategy_id", "default")

        # Ensure the position manager has a bucket for this strategy
        if strategy_id not in self._position_manager._positions:
            self._position_manager._positions[strategy_id] = {}

        synced_count = 0
        total_upnl = 0.0

        for pos in open_positions:
            sym = pos.get("symbol", "")
            contracts = float(pos.get("contracts", 0) or 0)
            side_str = (pos.get("side") or "").lower()
            signed_qty = contracts if side_str == "long" else -contracts
            entry_price = float(pos.get("entry_price", 0) or 0)
            mark_price = float(pos.get("mark_price", 0) or 0) or entry_price
            upnl = float(pos.get("unrealized_pnl", 0) or 0)
            total_upnl += upnl

            # ── 1 & 2: Strategy state ──
            old_pos = self._strategy.get_position(sym)
            restore_fn = getattr(self._strategy, "restore_position_state", None)
            if callable(restore_fn):
                restore_fn(
                    symbol=sym,
                    quantity=signed_qty,
                    avg_entry_price=entry_price,
                    current_price=mark_price,
                )
            else:
                # Fallback: minimal strategy sync (older strategies)
                self._strategy._positions[sym] = signed_qty

            # ── 3: Position manager ──
            pos_side = PositionSide.LONG if signed_qty > 0 else PositionSide.SHORT
            position_obj = Position(
                symbol=sym,
                strategy_id=strategy_id,
                side=pos_side,
                quantity=abs(signed_qty),
                average_entry_price=entry_price,
                current_price=mark_price,
                unrealized_pnl=upnl,
                realized_pnl=0.0,
                asset_class=AssetClass.CRYPTO,
            )
            self._position_manager._positions[strategy_id][sym] = position_obj

            synced_count += 1
            logger.info(
                "POSITION_SYNCED",
                symbol=sym,
                direction=pos_side.value,
                quantity=abs(signed_qty),
                entry_price=entry_price,
                mark_price=mark_price,
                unrealized_pnl=upnl,
                was=old_pos,
                has_state_restore=callable(restore_fn),
            )

        # Session tracking: we inherited existing trades, not new orders
        self._session_pnl = total_upnl
        self._session_trades = synced_count
        self._session_orders = 0

        logger.info(
            "exchange_position_sync_complete",
            synced_count=synced_count,
            total_unrealized_pnl=total_upnl,
        )

    # ------------------------------------------------------------------
    # Session PnL — realized accumulator + live unrealized
    # ------------------------------------------------------------------

    def _pm_realized_total(self) -> float:
        """Total realized PnL known to the PositionManager, including
        partially-closed open positions."""
        pm = self._position_manager
        if pm is None:
            return 0.0
        open_realized = sum(p.realized_pnl for p in pm.get_all_positions())
        return pm._total_realized_pnl + open_realized

    def _session_total_pnl(self) -> float:
        """Session PnL = realized-since-session-start + current unrealized.

        Realized losses ACCUMULATE — closing a losing trade must not reset
        the session loss meter (that reset is how the $15 cap silently
        never fired across multiple losing trades).
        """
        realized = self._pm_realized_total() - self._session_realized_baseline
        if self._exchange_snapshot:
            upnl = float(self._exchange_snapshot.get("unrealized_pnl") or 0.0)
        else:
            upnl = sum(
                p.unrealized_pnl
                for p in self._position_manager.get_all_positions()
            ) if self._position_manager else 0.0
        return realized + upnl

    # ------------------------------------------------------------------
    # Risk snapshot — exchange-truth equity
    # ------------------------------------------------------------------

    def _build_risk_snapshot(self) -> "PortfolioSnapshot":
        """Portfolio snapshot for risk checks, using EXCHANGE equity as truth.

        The internal PositionManager uses spot-style cash accounting: a SELL
        credits the full notional as cash, and get_snapshot() then ALSO adds
        the position's market value. For a leveraged perp short this inflates
        total_value ~5x (e.g. $86.98 equity → $468.15 fake peak). When the
        short closes, the fake equity collapses and the circuit breaker reads
        it as a huge drawdown — the root cause of the 2026-06-07 false
        "75.5% drawdown" emergency stop on a PROFITABLE trade.

        Fix: overlay exchange-reported equity (fetched every 15s in the main
        loop, always BEFORE the risk check) onto the snapshot. Falls back to
        the internal snapshot only if no exchange data has arrived yet.
        """
        internal = self._position_manager.get_snapshot()
        ex = self._exchange_snapshot
        if not ex or not ex.get("total_value"):
            return internal

        # Staleness guard: if the exchange snapshot hasn't refreshed for over
        # 120s (normal cadence: 15s), the equity may be hours old — risk
        # checks on stale equity are blind. Fall back to internal (now on
        # correct perp accounting) and warn loudly.
        age = time.time() - self._exchange_snapshot_time
        if age > 120:
            logger.warning(
                "risk_snapshot_stale_falling_back_internal",
                snapshot_age_sec=round(age, 1),
                internal_equity=round(internal.total_value, 2),
                stale_exchange_equity=round(float(ex["total_value"]), 2),
            )
            return internal

        return PortfolioSnapshot(
            timestamp=internal.timestamp,
            total_value=float(ex["total_value"]),
            cash=float(ex.get("cash") or ex["total_value"]),
            positions=internal.positions,
            unrealized_pnl=float(ex.get("unrealized_pnl") or 0.0),
            realized_pnl=internal.realized_pnl,
            daily_pnl=float(ex.get("daily_pnl") or 0.0),
        )

    # ------------------------------------------------------------------
    # Main Loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main live trading loop. Blocks until shutdown."""
        self._running = True
        self._session_start = datetime.now(timezone.utc)
        self._emergency_stopped = False
        # Session realized-PnL baseline: everything before this moment belongs
        # to previous sessions (sync_exchange_positions has already run).
        self._session_realized_baseline = self._pm_realized_total()

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
                # Realized + unrealized combined; realized losses accumulate
                # across trades instead of being erased on position close.
                session_total = self._session_total_pnl()
                self._session_pnl = session_total
                if session_total < -max_session_loss:
                    logger.critical(
                        "SESSION_LOSS_LIMIT_HIT",
                        session_pnl=session_total,
                        session_realized=self._pm_realized_total()
                                         - self._session_realized_baseline,
                        limit=max_session_loss,
                    )
                    await self._emergency_stop("Session loss limit reached")
                    break

                # ── Phase 6: Risk monitoring ──
                # Uses exchange-truth equity, NOT internal spot accounting
                # (see _build_risk_snapshot — prevents false drawdown kills).
                snapshot = self._build_risk_snapshot()
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

        # Get exchange market info — missing info means we cannot validate
        # size at all; submitting blind gets rejected by the exchange anyway.
        market_info = self._engine.get_market_info(symbol)
        if not market_info:
            logger.error("MARKET_INFO_MISSING", symbol=symbol)
            return 0.0, {"skipped": True, "reason": "market_info_unavailable"}
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
            # (exchange typically allows closing any position).
            # NEVER force-round an exit UP to min lot: if internal qty is
            # stale/dust and the exchange holds less, an oversized reduce_only
            # is rejected and the position stays stuck open. Skipping lets
            # reconciliation self-heal handle the drift instead.
            qty = self._engine.amount_to_precision(symbol, qty)
            if qty <= 0:
                logger.warning(
                    "EXIT_BELOW_PRECISION_SKIPPED",
                    symbol=symbol,
                    raw_qty=abs(raw_qty),
                    hint="dust exit deferred to reconciliation",
                )
                return 0.0, {"skipped": True, "reason": "exit_below_precision"}
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

        # Keep internal mark-to-market current — without this, unrealized PnL
        # stays frozen at the entry price (audit P1-7).
        self._position_manager.update_prices({symbol: bar.close})

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
                if exit_qty <= 0:
                    logger.warning(
                        "EXIT_SKIPPED", symbol=target.symbol,
                        reason=exit_meta.get("reason", "adjusted_to_zero"),
                    )
                    continue

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

            # Pre-trade risk check (exchange-truth equity, same as Phase 6).
            # ref_price: MARKET orders carry no limit_price — without a market
            # price the notional checks would value every order at $0.
            snapshot = self._build_risk_snapshot()
            approved, reason = self._risk_manager.check_order(
                order, snapshot, ref_price=bar.close,
            )
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

            # Persist to DB — audit trail + crash recovery. Failure-tolerant:
            # a DB hiccup must never stop live trading (fills were previously
            # memory-only; a crash erased all trade history — audit P1-9).
            try:
                from core.database import get_session
                from core.database.repository import FillRepo
                async with get_session() as session:
                    await FillRepo.insert(
                        session,
                        fill_id=str(fill.fill_id),
                        order_id=str(fill.order_id),
                        symbol=fill.symbol,
                        side=fill.side.value,
                        price=fill.price,
                        quantity=fill.quantity,
                        commission=fill.commission,
                        strategy_id=fill.strategy_id,
                        asset_class=getattr(fill.asset_class, "value", "crypto"),
                        exchange=self._config.get("exchange", {}).get("id"),
                        timestamp=fill.timestamp,
                    )
                    await session.commit()
            except Exception as db_err:
                logger.warning("fill_db_persist_failed", error=str(db_err),
                               fill_id=str(fill.fill_id))

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
                    streak = self._mismatch_streak.get(sym, 0) + 1
                    self._mismatch_streak[sym] = streak
                    logger.warning(
                        "POSITION_MISMATCH",
                        symbol=sym,
                        exchange_qty=exchange_qty,
                        internal_qty=internal_qty,
                        diff=diff,
                        consecutive=streak,
                    )
                    # Self-heal: after 3 consecutive mismatches (~3 min) the
                    # drift is persistent, not a fill-in-flight. Exchange is
                    # the source of truth — force internal state to match it.
                    # (The 6-02 ghost position sat un-healed for hours because
                    # this loop only ever warned.)
                    if streak >= 3:
                        await self._force_sync_position(sym, exchange_qty)
                        self._mismatch_streak[sym] = 0
                else:
                    self._mismatch_streak.pop(sym, None)

        except Exception as e:
            logger.warning("reconciliation_error", error=str(e))

    async def _force_sync_position(self, symbol: str, exchange_qty: float) -> None:
        """Overwrite internal position state with the exchange's truth."""
        strategy_id = self._config.get("strategy_id", "default")
        pm_positions = self._position_manager._positions.setdefault(strategy_id, {})

        if abs(exchange_qty) < 1e-12:
            # Exchange is flat → drop the internal phantom.
            pm_positions.pop(symbol, None)
            self._strategy._positions[symbol] = 0.0
            logger.critical(
                "POSITION_AUTO_CORRECTED", symbol=symbol,
                action="cleared_internal_phantom", exchange_qty=0.0,
            )
            return

        # Exchange has a position we're not tracking (or wrong size) —
        # rebuild from detailed exchange data (entry price, mark, upnl).
        try:
            detailed = await self._engine.get_positions_detailed()
        except Exception as e:
            logger.error("force_sync_fetch_failed", symbol=symbol, error=str(e))
            return
        pos_data = next((p for p in detailed if p.get("symbol") == symbol), None)
        if pos_data is None:
            logger.error("force_sync_position_missing", symbol=symbol)
            return

        entry_price = float(pos_data.get("entry_price", 0) or 0)
        mark_price = float(pos_data.get("mark_price", 0) or 0) or entry_price
        pos_side = PositionSide.LONG if exchange_qty > 0 else PositionSide.SHORT
        pm_positions[symbol] = Position(
            symbol=symbol,
            strategy_id=strategy_id,
            side=pos_side,
            quantity=abs(exchange_qty),
            average_entry_price=entry_price,
            current_price=mark_price,
            unrealized_pnl=float(pos_data.get("unrealized_pnl", 0) or 0),
            realized_pnl=0.0,
            asset_class=AssetClass.CRYPTO_PERP,
        )
        restore_fn = getattr(self._strategy, "restore_position_state", None)
        if callable(restore_fn):
            restore_fn(symbol=symbol, quantity=exchange_qty,
                       avg_entry_price=entry_price, current_price=mark_price)
        else:
            self._strategy._positions[symbol] = exchange_qty
        logger.critical(
            "POSITION_AUTO_CORRECTED", symbol=symbol,
            action="rebuilt_from_exchange", exchange_qty=exchange_qty,
            entry_price=entry_price,
        )

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

            # VERIFY the closes actually landed — submitting is not closing.
            # Poll the exchange up to 30s; if anything is still open, scream.
            for _ in range(6):
                await asyncio.sleep(5)
                try:
                    remaining = await self._engine.get_positions()
                    still_open = {s: q for s, q in remaining.items() if abs(q) > 1e-10}
                    if not still_open:
                        logger.info("emergency_close_verified_flat")
                        break
                except Exception as e:
                    logger.warning("emergency_close_verify_failed", error=str(e))
            else:
                logger.critical(
                    "EMERGENCY_CLOSE_INCOMPLETE",
                    still_open=still_open,
                    message="Positions remain open on exchange after emergency "
                            "stop — MANUAL INTERVENTION REQUIRED",
                )
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

            session_realized = (
                self._pm_realized_total() - self._session_realized_baseline
            )
            self._exchange_snapshot = {
                "total_value": equity,
                "cash": cash,
                "unrealized_pnl": total_upnl,
                "realized_pnl": session_realized,
                "daily_pnl": equity - self._position_manager._initial_cash if self._position_manager else 0,
                "num_positions": len(positions),
                "positions": positions,
            }
            self._exchange_snapshot_time = time.time()

            # Display PnL = accumulated realized + live unrealized.
            # (The old `_session_pnl = total_upnl` overwrite erased realized
            # losses and neutered the session loss cap — audit P0-3.)
            self._session_pnl = session_realized + total_upnl

        except Exception as e:
            age = (time.time() - self._exchange_snapshot_time
                   if self._exchange_snapshot_time else None)
            logger.warning(
                "exchange_snapshot_update_failed",
                error=str(e),
                snapshot_age_sec=round(age, 1) if age is not None else None,
            )
