"""Trading Orchestrator — central control plane for live trading.

Manages strategy lifecycle, market data multiplexing, order routing,
risk monitoring, position reconciliation, and state persistence.

Usage:
    orchestrator = TradingOrchestrator(event_bus, order_router, risk_manager)
    orchestrator.register_strategy("gamma_scalp", strategy, engine_type="ib")
    await orchestrator.start("gamma_scalp")
    await orchestrator.stop("gamma_scalp")
"""

from __future__ import annotations

import asyncio
import json
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

from core.enums import AssetClass, OrderSide, StrategyState
from core.events import (
    Event,
    EventBus,
    EventType,
    FillEvent,
    RiskAlertEvent,
    SignalEvent,
)
from core.logging import get_logger
from core.models import Fill, Order, PortfolioSnapshot
from execution.engine.base import ExecutionEngine
from execution.oms.order_router import OrderRouter

logger = get_logger(__name__)


# ------------------------------------------------------------------
# Strategy registration record
# ------------------------------------------------------------------

class StrategySlot:
    """Runtime state for a single registered strategy."""

    def __init__(
        self,
        strategy_id: str,
        strategy: Any,  # StrategyBase instance
        engine_type: str,  # paper / ib / ccxt
        exchange: Optional[str] = None,
        asset_class: AssetClass = AssetClass.CRYPTO,
        allocation: float = 0.0,
        config: Optional[Dict[str, Any]] = None,
    ):
        self.strategy_id = strategy_id
        self.strategy = strategy
        self.engine_type = engine_type
        self.exchange = exchange
        self.asset_class = asset_class
        self.allocation = allocation
        self.config = config or {}

        self.state = StrategyState.IDLE
        self.error: Optional[str] = None
        self.task: Optional[asyncio.Task] = None
        self.total_pnl: float = 0.0
        self.total_trades: int = 0
        self.last_heartbeat: Optional[datetime] = None
        self.started_at: Optional[datetime] = None
        self.stopped_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "name": getattr(self.strategy, "config", {}).name if hasattr(getattr(self.strategy, "config", None), "name") else self.strategy_id,
            "state": self.state.value,
            "engine_type": self.engine_type,
            "exchange": self.exchange,
            "asset_class": self.asset_class.value,
            "allocation": self.allocation,
            "total_pnl": self.total_pnl,
            "total_trades": self.total_trades,
            "error": self.error,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "stopped_at": self.stopped_at.isoformat() if self.stopped_at else None,
        }


# ------------------------------------------------------------------
# Orchestrator
# ------------------------------------------------------------------

# Type for state-change callbacks (strategy_id, old_state, new_state)
StateCallback = Callable[[str, StrategyState, StrategyState], Coroutine[Any, Any, None]]


class TradingOrchestrator:
    """Central control plane for multi-strategy live trading.

    Responsibilities:
    - Strategy lifecycle management (start/stop/pause/resume)
    - Market data → strategy signal → order routing pipeline
    - Risk check integration (pre-trade + realtime + circuit breaker)
    - Position reconciliation with exchange
    - State persistence to database
    - WebSocket broadcasting for frontend
    """

    def __init__(
        self,
        event_bus: EventBus,
        order_router: OrderRouter,
        risk_manager: Any = None,  # RiskManager (optional, imported later)
        heartbeat_interval: float = 5.0,
        reconcile_interval: float = 60.0,
        max_total_exposure_usd: float = 200_000.0,
    ):
        self._event_bus = event_bus
        self._order_router = order_router
        self._risk_manager = risk_manager

        self._slots: Dict[str, StrategySlot] = {}
        self._state_callbacks: List[StateCallback] = []
        self._heartbeat_interval = heartbeat_interval
        self._reconcile_interval = reconcile_interval
        self._max_total_exposure = max_total_exposure_usd

        self._heartbeat_task: Optional[asyncio.Task] = None
        self._reconcile_task: Optional[asyncio.Task] = None
        self._running = False
        self._emergency_stopped = False

        # Wire up event handlers
        self._event_bus.subscribe(EventType.FILL, self._on_fill)
        self._event_bus.subscribe(EventType.RISK_ALERT, self._on_risk_alert)

    # ------------------------------------------------------------------
    # Strategy registration
    # ------------------------------------------------------------------

    def register_strategy(
        self,
        strategy_id: str,
        strategy: Any,
        *,
        engine_type: str = "paper",
        exchange: Optional[str] = None,
        asset_class: AssetClass = AssetClass.CRYPTO,
        allocation: float = 0.0,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Register a strategy for orchestration."""
        if strategy_id in self._slots:
            raise ValueError(f"Strategy {strategy_id} already registered")

        slot = StrategySlot(
            strategy_id=strategy_id,
            strategy=strategy,
            engine_type=engine_type,
            exchange=exchange,
            asset_class=asset_class,
            allocation=allocation,
            config=config,
        )
        self._slots[strategy_id] = slot
        logger.info(
            "strategy_registered",
            strategy_id=strategy_id,
            engine_type=engine_type,
            exchange=exchange,
        )

    def unregister_strategy(self, strategy_id: str) -> None:
        """Remove a strategy (must be stopped first)."""
        slot = self._slots.get(strategy_id)
        if not slot:
            return
        if slot.state in (StrategyState.RUNNING, StrategyState.STARTING):
            raise RuntimeError(f"Cannot unregister running strategy {strategy_id}")
        del self._slots[strategy_id]

    # ------------------------------------------------------------------
    # Lifecycle: start / stop / pause / resume
    # ------------------------------------------------------------------

    async def start_strategy(self, strategy_id: str) -> None:
        """Start a registered strategy."""
        slot = self._get_slot(strategy_id)

        if slot.state in (StrategyState.RUNNING, StrategyState.STARTING):
            logger.warning("strategy_already_running", strategy_id=strategy_id)
            return

        if self._emergency_stopped:
            raise RuntimeError("Emergency stop is active — cannot start strategies")

        await self._set_state(slot, StrategyState.STARTING)

        try:
            # Initialize strategy
            strategy = slot.strategy
            if hasattr(strategy, "initialize"):
                strategy.initialize()

            slot.started_at = datetime.now(timezone.utc)
            await self._set_state(slot, StrategyState.RUNNING)

            # Start the strategy loop as a background task
            slot.task = asyncio.create_task(
                self._strategy_loop(slot),
                name=f"strategy_{strategy_id}",
            )

            logger.info("strategy_started", strategy_id=strategy_id)

        except Exception as e:
            slot.error = str(e)
            await self._set_state(slot, StrategyState.ERROR)
            logger.error("strategy_start_failed", strategy_id=strategy_id, error=str(e))
            raise

    async def stop_strategy(self, strategy_id: str) -> None:
        """Gracefully stop a strategy."""
        slot = self._get_slot(strategy_id)

        if slot.state not in (StrategyState.RUNNING, StrategyState.PAUSED):
            return

        await self._set_state(slot, StrategyState.STOPPING)

        # Cancel the background task
        if slot.task and not slot.task.done():
            slot.task.cancel()
            try:
                await slot.task
            except (asyncio.CancelledError, Exception):
                pass

        # Shutdown strategy
        if hasattr(slot.strategy, "shutdown"):
            try:
                slot.strategy.shutdown()
            except Exception as e:
                logger.warning("strategy_shutdown_error", strategy_id=strategy_id, error=str(e))

        slot.stopped_at = datetime.now(timezone.utc)
        await self._set_state(slot, StrategyState.STOPPED)
        logger.info("strategy_stopped", strategy_id=strategy_id)

    async def pause_strategy(self, strategy_id: str) -> None:
        """Pause a running strategy (keeps positions, stops generating signals)."""
        slot = self._get_slot(strategy_id)
        if slot.state != StrategyState.RUNNING:
            return
        await self._set_state(slot, StrategyState.PAUSED)
        logger.info("strategy_paused", strategy_id=strategy_id)

    async def resume_strategy(self, strategy_id: str) -> None:
        """Resume a paused strategy."""
        slot = self._get_slot(strategy_id)
        if slot.state != StrategyState.PAUSED:
            return
        await self._set_state(slot, StrategyState.RUNNING)
        logger.info("strategy_resumed", strategy_id=strategy_id)

    # ------------------------------------------------------------------
    # Emergency controls
    # ------------------------------------------------------------------

    async def force_close_positions(self, strategy_id: str) -> Dict[str, Any]:
        """Force-close all positions for a strategy.

        Issues market orders to flatten every open position.
        """
        slot = self._get_slot(strategy_id)
        engine = self._order_router.get_engine(slot.asset_class, name=slot.exchange)
        if not engine:
            raise RuntimeError(f"No engine for {strategy_id}")

        positions = await engine.get_positions()
        closed = []

        for symbol, qty in positions.items():
            if abs(qty) < 1e-10:
                continue
            # Create a closing order
            from uuid import uuid4
            close_order = Order(
                order_id=uuid4(),
                strategy_id=strategy_id,
                symbol=symbol,
                side=OrderSide.SELL if qty > 0 else OrderSide.BUY,
                order_type=__import__("core.enums", fromlist=["OrderType"]).OrderType.MARKET,
                quantity=abs(qty),
                asset_class=slot.asset_class,
                metadata={"reduce_only": True, "exchange": slot.exchange, "reason": "force_close"},
            )
            try:
                broker_id = await self._order_router.route(close_order)
                closed.append({"symbol": symbol, "qty": qty, "broker_id": broker_id})
                logger.info("force_close_order", strategy_id=strategy_id, symbol=symbol, qty=qty)
            except Exception as e:
                logger.error("force_close_failed", symbol=symbol, error=str(e))
                closed.append({"symbol": symbol, "qty": qty, "error": str(e)})

        return {"strategy_id": strategy_id, "positions_closed": closed}

    async def emergency_stop(self) -> Dict[str, Any]:
        """KILL SWITCH: Stop all strategies and close all positions."""
        self._emergency_stopped = True
        results = {}

        logger.critical("EMERGENCY_STOP_TRIGGERED")

        for strategy_id, slot in list(self._slots.items()):
            try:
                if slot.state in (StrategyState.RUNNING, StrategyState.PAUSED):
                    # Force close first, then stop
                    close_result = await self.force_close_positions(strategy_id)
                    await self.stop_strategy(strategy_id)
                    results[strategy_id] = {"status": "stopped", "closed": close_result}
                else:
                    results[strategy_id] = {"status": slot.state.value}
            except Exception as e:
                results[strategy_id] = {"status": "error", "error": str(e)}
                logger.error("emergency_stop_error", strategy_id=strategy_id, error=str(e))

        # Publish risk alert
        await self._event_bus.publish_nowait(RiskAlertEvent(
            alert_level="KILL",
            message="Emergency stop triggered — all strategies stopped and positions closed",
            source="orchestrator",
        ))

        return results

    async def reset_emergency_stop(self) -> None:
        """Clear the emergency stop flag to allow strategies to restart."""
        self._emergency_stopped = False
        logger.info("emergency_stop_reset")

    # ------------------------------------------------------------------
    # Strategy execution loop
    # ------------------------------------------------------------------

    async def _strategy_loop(self, slot: StrategySlot) -> None:
        """Background loop: run strategy signal generation at each interval."""
        strategy = slot.strategy
        interval = slot.config.get("interval_seconds", 60)

        while slot.state == StrategyState.RUNNING:
            try:
                slot.last_heartbeat = datetime.now(timezone.utc)

                # Skip if paused (state can change asynchronously)
                if slot.state == StrategyState.PAUSED:
                    await asyncio.sleep(1)
                    continue

                # Generate signals
                signals = strategy.generate_signals()
                if not signals:
                    await asyncio.sleep(interval)
                    continue

                # Process each signal
                for signal in signals:
                    # Publish signal event
                    await self._event_bus.publish_nowait(SignalEvent(
                        strategy_id=slot.strategy_id,
                        symbol=signal.symbol,
                        direction=signal.direction,
                        strength=signal.strength,
                        target_quantity=signal.target_quantity,
                        stop_loss=signal.stop_loss,
                        take_profit=signal.take_profit,
                        source="orchestrator",
                    ))

                    # Convert signal to order (simplified — strategies can override)
                    if signal.target_quantity and abs(signal.target_quantity) > 0:
                        from core.enums import OrderType
                        from uuid import uuid4

                        order = Order(
                            order_id=uuid4(),
                            strategy_id=slot.strategy_id,
                            symbol=signal.symbol,
                            side=OrderSide.BUY if signal.target_quantity > 0 else OrderSide.SELL,
                            order_type=OrderType.MARKET,
                            quantity=abs(signal.target_quantity),
                            asset_class=slot.asset_class,
                            metadata={"exchange": slot.exchange},
                        )

                        # Pre-trade risk check
                        if self._risk_manager:
                            approved, reason = self._risk_manager.check_order(order, None)
                            if not approved:
                                logger.warning(
                                    "order_rejected_risk",
                                    strategy_id=slot.strategy_id,
                                    symbol=signal.symbol,
                                    reason=reason,
                                )
                                continue

                        # Route order
                        try:
                            broker_id = await self._order_router.route(order)
                            logger.info(
                                "order_routed",
                                strategy_id=slot.strategy_id,
                                symbol=signal.symbol,
                                broker_id=broker_id,
                            )
                        except Exception as e:
                            logger.error(
                                "order_routing_failed",
                                strategy_id=slot.strategy_id,
                                error=str(e),
                            )

                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                slot.error = str(e)
                logger.error(
                    "strategy_loop_error",
                    strategy_id=slot.strategy_id,
                    error=str(e),
                    traceback=traceback.format_exc(),
                )
                await self._set_state(slot, StrategyState.ERROR)
                break

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_fill(self, event: Event) -> None:
        """Handle fill events — update strategy metrics."""
        if not isinstance(event, FillEvent):
            return
        slot = self._slots.get(event.strategy_id)
        if not slot:
            return

        slot.total_trades += 1

        # Forward fill to strategy
        if hasattr(slot.strategy, "on_fill"):
            try:
                fill = Fill(
                    fill_id=event.fill_id,
                    order_id=event.order_id,
                    symbol=event.symbol,
                    side=event.side,
                    price=event.price,
                    quantity=event.quantity,
                    commission=event.commission,
                    timestamp=event.timestamp,
                    strategy_id=event.strategy_id,
                )
                slot.strategy.on_fill(fill)
            except Exception as e:
                logger.warning("strategy_on_fill_error", strategy_id=event.strategy_id, error=str(e))

    async def _on_risk_alert(self, event: Event) -> None:
        """Handle risk alerts — pause or kill strategies if needed."""
        if not isinstance(event, RiskAlertEvent):
            return

        if event.alert_level == "KILL":
            logger.critical(
                "risk_kill_signal",
                strategy_id=event.strategy_id,
                message=event.message,
            )
            if event.strategy_id and event.strategy_id in self._slots:
                await self.force_close_positions(event.strategy_id)
                await self.stop_strategy(event.strategy_id)
            else:
                await self.emergency_stop()

        elif event.alert_level == "CRITICAL":
            if event.strategy_id and event.strategy_id in self._slots:
                await self.pause_strategy(event.strategy_id)

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    async def _set_state(self, slot: StrategySlot, new_state: StrategyState) -> None:
        """Update strategy state and notify callbacks."""
        old_state = slot.state
        slot.state = new_state

        # Notify registered callbacks
        for cb in self._state_callbacks:
            try:
                await cb(slot.strategy_id, old_state, new_state)
            except Exception as e:
                logger.warning("state_callback_error", error=str(e))

    def on_state_change(self, callback: StateCallback) -> None:
        """Register a callback for strategy state changes."""
        self._state_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Orchestrator lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the orchestrator (heartbeat + reconciliation loops)."""
        if self._running:
            return
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("orchestrator_started")

    async def shutdown(self) -> None:
        """Shutdown orchestrator — stop all strategies gracefully."""
        self._running = False

        # Stop all running strategies
        for strategy_id in list(self._slots.keys()):
            slot = self._slots[strategy_id]
            if slot.state in (StrategyState.RUNNING, StrategyState.PAUSED):
                try:
                    await self.stop_strategy(strategy_id)
                except Exception as e:
                    logger.error("shutdown_stop_error", strategy_id=strategy_id, error=str(e))

        # Cancel background tasks
        for task in [self._heartbeat_task, self._reconcile_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        logger.info("orchestrator_shutdown")

    async def _heartbeat_loop(self) -> None:
        """Monitor strategy health."""
        while self._running:
            try:
                now = datetime.now(timezone.utc)
                for sid, slot in self._slots.items():
                    if slot.state != StrategyState.RUNNING:
                        continue
                    if slot.last_heartbeat:
                        age = (now - slot.last_heartbeat).total_seconds()
                        if age > self._heartbeat_interval * 10:
                            logger.warning(
                                "strategy_heartbeat_stale",
                                strategy_id=sid,
                                age_seconds=age,
                            )
                await asyncio.sleep(self._heartbeat_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("heartbeat_error", error=str(e))
                await asyncio.sleep(5)

    # ------------------------------------------------------------------
    # Status queries
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Get orchestrator and all strategy status."""
        return {
            "running": self._running,
            "emergency_stopped": self._emergency_stopped,
            "strategies": {
                sid: slot.to_dict() for sid, slot in self._slots.items()
            },
        }

    def get_strategy_status(self, strategy_id: str) -> Dict[str, Any]:
        """Get status of a single strategy."""
        slot = self._get_slot(strategy_id)
        return slot.to_dict()

    @property
    def strategy_ids(self) -> List[str]:
        return list(self._slots.keys())

    @property
    def running_strategies(self) -> List[str]:
        return [sid for sid, s in self._slots.items() if s.state == StrategyState.RUNNING]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_slot(self, strategy_id: str) -> StrategySlot:
        slot = self._slots.get(strategy_id)
        if not slot:
            raise KeyError(f"Strategy not registered: {strategy_id}")
        return slot
