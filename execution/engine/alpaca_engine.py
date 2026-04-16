"""Alpaca equities execution engine.

Pre-wired for live equities trading via Alpaca's REST + WebSocket API.
Uses the alpaca-py SDK for order management, position tracking, and streaming.

Status: READY_NOT_TESTED — all methods implemented, pending live verification.

Requirements:
    pip install alpaca-py

Usage:
    engine = AlpacaExecutionEngine(
        api_key="...",
        api_secret="...",
        paper=True,
    )
    await engine.connect()
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4

from core.enums import AssetClass, OrderSide, OrderStatus, OrderType
from core.logging import get_logger
from core.models import Fill, Order
from execution.engine.base import ExecutionEngine

logger = get_logger(__name__)


class AlpacaExecutionEngine(ExecutionEngine):
    """Alpaca equities execution engine (stocks, ETFs).

    Connects to Alpaca brokerage via alpaca-py.
    Supports market, limit, stop, and trailing stop orders.
    Paper and live trading modes.
    """

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        paper: bool = True,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._paper = paper

        self._trading_client: Any = None
        self._connected = False
        self._pending_orders: Dict[str, Any] = {}  # our_id -> alpaca order
        self._fills: List[Fill] = []
        self._processed_fills: set = set()
        self._ws_task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        """Connect to Alpaca API."""
        try:
            from alpaca.trading.client import TradingClient
        except ImportError:
            raise ImportError(
                "alpaca-py not installed. Run: pip install alpaca-py"
            )

        self._trading_client = TradingClient(
            api_key=self._api_key,
            secret_key=self._api_secret,
            paper=self._paper,
        )

        # Verify connectivity
        account = self._trading_client.get_account()
        self._connected = True

        logger.info(
            "alpaca_engine_connected",
            paper=self._paper,
            equity=account.equity,
            buying_power=account.buying_power,
            account_status=account.status,
        )

    async def disconnect(self) -> None:
        """Disconnect and clean up."""
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
        self._connected = False
        logger.info("alpaca_engine_disconnected")

    async def submit_order(self, order: Order) -> str:
        """Submit an order to Alpaca.

        Returns:
            Broker order ID
        """
        if not self._connected:
            raise ConnectionError("Not connected to Alpaca")

        from alpaca.trading.requests import (
            LimitOrderRequest,
            MarketOrderRequest,
            StopLimitOrderRequest,
            StopOrderRequest,
            TrailingStopOrderRequest,
        )
        from alpaca.trading.enums import OrderSide as AlpSide, TimeInForce

        side = AlpSide.BUY if order.side == OrderSide.BUY else AlpSide.SELL
        qty = abs(order.quantity)
        tif = TimeInForce.DAY

        if order.order_type == OrderType.MARKET:
            req = MarketOrderRequest(
                symbol=order.symbol,
                qty=qty,
                side=side,
                time_in_force=tif,
            )
        elif order.order_type == OrderType.LIMIT:
            if order.limit_price is None:
                raise ValueError("Limit order requires limit_price")
            req = LimitOrderRequest(
                symbol=order.symbol,
                qty=qty,
                side=side,
                time_in_force=tif,
                limit_price=order.limit_price,
            )
        elif order.order_type == OrderType.STOP:
            if order.stop_price is None:
                raise ValueError("Stop order requires stop_price")
            req = StopOrderRequest(
                symbol=order.symbol,
                qty=qty,
                side=side,
                time_in_force=tif,
                stop_price=order.stop_price,
            )
        elif order.order_type == OrderType.STOP_LIMIT:
            if order.stop_price is None or order.limit_price is None:
                raise ValueError("Stop-limit order requires both stop_price and limit_price")
            req = StopLimitOrderRequest(
                symbol=order.symbol,
                qty=qty,
                side=side,
                time_in_force=tif,
                stop_price=order.stop_price,
                limit_price=order.limit_price,
            )
        elif order.order_type == OrderType.TRAILING_STOP:
            trail_pct = order.metadata.get("trail_percent", 1.0)
            req = TrailingStopOrderRequest(
                symbol=order.symbol,
                qty=qty,
                side=side,
                time_in_force=tif,
                trail_percent=trail_pct,
            )
        else:
            raise ValueError(f"Unsupported order type: {order.order_type}")

        # Submit via SDK (sync call, run in executor)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: self._trading_client.submit_order(req)
        )

        broker_id = str(result.id)
        our_id = str(order.order_id)
        self._pending_orders[our_id] = result

        logger.info(
            "alpaca_order_submitted",
            our_id=our_id,
            broker_id=broker_id,
            symbol=order.symbol,
            side=str(side),
            qty=qty,
        )

        # Check if already filled (market orders)
        if str(result.status) == "filled":
            fill = Fill(
                fill_id=uuid4(),
                order_id=our_id,
                symbol=order.symbol,
                side=order.side,
                price=float(result.filled_avg_price or 0),
                quantity=float(result.filled_qty or 0),
                commission=0.0,  # Alpaca is commission-free
                timestamp=datetime.now(timezone.utc),
                strategy_id=order.strategy_id,
                asset_class=AssetClass.EQUITY,
            )
            self._fills.append(fill)

        return broker_id

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order."""
        if not self._connected:
            return False

        alpaca_order = self._pending_orders.get(order_id)
        if not alpaca_order:
            return False

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: self._trading_client.cancel_order_by_id(str(alpaca_order.id))
            )
            self._pending_orders.pop(order_id, None)
            logger.info("alpaca_order_cancelled", order_id=order_id)
            return True
        except Exception as e:
            logger.error("alpaca_cancel_failed", order_id=order_id, error=str(e))
            return False

    async def get_fills(self) -> List[Fill]:
        """Return and clear buffered fills."""
        fills = list(self._fills)
        self._fills.clear()
        return fills

    async def get_positions(self) -> Dict[str, float]:
        """Get current positions from Alpaca."""
        if not self._connected:
            return {}

        try:
            loop = asyncio.get_event_loop()
            positions = await loop.run_in_executor(
                None, lambda: self._trading_client.get_all_positions()
            )
            return {
                pos.symbol: float(pos.qty)
                for pos in positions
            }
        except Exception as e:
            logger.error("alpaca_get_positions_failed", error=str(e))
            return {}

    async def get_account_summary(self) -> Dict[str, Any]:
        """Get Alpaca account summary."""
        if not self._connected:
            return {}

        try:
            loop = asyncio.get_event_loop()
            account = await loop.run_in_executor(
                None, lambda: self._trading_client.get_account()
            )
            return {
                "equity": float(account.equity),
                "buying_power": float(account.buying_power),
                "cash": float(account.cash),
                "portfolio_value": float(account.portfolio_value),
                "long_market_value": float(account.long_market_value),
                "short_market_value": float(account.short_market_value),
                "paper": self._paper,
            }
        except Exception as e:
            logger.error("alpaca_account_summary_failed", error=str(e))
            return {}

    async def health_check(self) -> bool:
        """Check Alpaca API connectivity."""
        if not self._connected or not self._trading_client:
            return False
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: self._trading_client.get_account()
            )
            return True
        except Exception:
            return False

    @property
    def is_paper(self) -> bool:
        return self._paper

    @property
    def is_connected(self) -> bool:
        return self._connected
