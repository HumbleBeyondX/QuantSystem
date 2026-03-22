"""Interactive Brokers live execution engine via ib_insync.

Supports stocks and options order execution through IB TWS/Gateway.
Works with both paper trading (port 7497) and live trading (port 7496).

Requirements:
    pip install ib_insync

Setup:
    1. Download & install IB TWS or IB Gateway
    2. Enable API connections in TWS:
       Configure -> API -> Settings -> Enable ActiveX and Socket Clients
    3. For paper trading: use port 7497
    4. For live trading: use port 7496 (Phase 5)
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import uuid4

from core.enums import AssetClass, OrderSide, OrderStatus, OrderType
from core.logging import get_logger
from core.models import Fill, Order
from execution.engine.base import ExecutionEngine

logger = get_logger(__name__)


class IBExecutionEngine(ExecutionEngine):
    """Interactive Brokers execution engine for stocks + options.

    Connects to IB TWS/Gateway via ib_insync. Supports:
      - Market, Limit, Stop orders for stocks
      - Options order submission (single leg)
      - Multi-leg combo orders (spreads, straddles)
      - Real-time order status tracking
      - Position reconciliation
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,  # 7497=paper, 7496=live
        client_id: int = 10,
        timeout: int = 30,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._timeout = timeout
        self._ib = None
        self._connected = False
        self._pending_orders: Dict[str, Any] = {}  # our_id -> ib_trade
        self._fills: List[Fill] = []
        self._order_map: Dict[str, int] = {}  # our_id -> ib_order_id

    async def connect(self) -> None:
        """Connect to IB TWS/Gateway."""
        try:
            from ib_insync import IB
            self._ib = IB()

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: self._ib.connect(
                    self._host, self._port,
                    clientId=self._client_id,
                    timeout=self._timeout,
                ),
            )

            self._connected = True

            # Log account info
            accounts = self._ib.managedAccounts()
            account_values = self._ib.accountSummary()
            equity = 0
            for av in account_values:
                if av.tag == "NetLiquidation":
                    equity = float(av.value)
                    break

            logger.info(
                "ib_engine_connected",
                host=self._host,
                port=self._port,
                accounts=accounts,
                equity=equity,
                paper=(self._port == 7497),
            )
            print(f"  [IB] Connected to {'PAPER' if self._port == 7497 else 'LIVE'} trading")
            print(f"  [IB] Accounts: {accounts}")
            print(f"  [IB] Net Liquidation: ${equity:,.2f}")

        except ImportError:
            raise ImportError(
                "ib_insync not installed. Run: pip install ib_insync"
            )
        except Exception as e:
            logger.error("ib_connection_failed", error=str(e))
            raise ConnectionError(
                f"Failed to connect to IB TWS at {self._host}:{self._port}. "
                f"Ensure TWS/Gateway is running with API enabled. Error: {e}"
            )

    async def disconnect(self) -> None:
        """Disconnect from IB."""
        if self._ib and self._connected:
            self._ib.disconnect()
            self._connected = False
            logger.info("ib_engine_disconnected")

    async def submit_order(self, order: Order) -> str:
        """Submit an order to IB.

        Args:
            order: Order object with symbol, side, quantity, order_type, etc.

        Returns:
            Broker order ID string
        """
        if not self._connected:
            raise ConnectionError("Not connected to IB")

        from ib_insync import Stock, MarketOrder, LimitOrder, StopOrder

        # Create IB contract
        contract = Stock(order.symbol, "SMART", "USD")

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: self._ib.qualifyContracts(contract)
        )

        # Create IB order
        action = "BUY" if order.side == OrderSide.BUY else "SELL"
        qty = abs(order.quantity)

        if order.order_type == OrderType.MARKET:
            ib_order = MarketOrder(action, qty)
        elif order.order_type == OrderType.LIMIT:
            if order.limit_price is None:
                raise ValueError("Limit order requires limit_price")
            ib_order = LimitOrder(action, qty, order.limit_price)
        elif order.order_type == OrderType.STOP:
            if order.stop_price is None:
                raise ValueError("Stop order requires stop_price")
            ib_order = StopOrder(action, qty, order.stop_price)
        else:
            raise ValueError(f"Unsupported order type: {order.order_type}")

        # Submit
        trade = await loop.run_in_executor(
            None, lambda: self._ib.placeOrder(contract, ib_order)
        )

        broker_id = str(trade.order.orderId)
        our_id = str(order.order_id)
        self._pending_orders[our_id] = trade
        self._order_map[our_id] = trade.order.orderId

        logger.info(
            "ib_order_submitted",
            our_id=our_id,
            broker_id=broker_id,
            symbol=order.symbol,
            side=action,
            qty=qty,
            order_type=str(order.order_type),
        )

        # Wait briefly for fill (market orders usually fill immediately)
        if order.order_type == OrderType.MARKET:
            await asyncio.sleep(0.5)
            await loop.run_in_executor(None, lambda: self._ib.sleep(0.1))

        return broker_id

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order."""
        if not self._connected:
            return False

        trade = self._pending_orders.get(order_id)
        if not trade:
            return False

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self._ib.cancelOrder(trade.order))
        logger.info("ib_order_cancelled", order_id=order_id)
        return True

    async def get_fills(self) -> List[Fill]:
        """Get new fills since last check."""
        if not self._connected:
            return []

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: self._ib.sleep(0.1))

        new_fills = []
        completed = []

        for our_id, trade in self._pending_orders.items():
            for ib_fill in trade.fills:
                fill_key = f"{our_id}_{ib_fill.execution.execId}"
                # Deduplicate
                if fill_key not in self._processed_fills:
                    self._processed_fills.add(fill_key)

                    fill = Fill(
                        fill_id=uuid4(),
                        order_id=our_id,
                        symbol=trade.contract.symbol,
                        side=OrderSide.BUY if ib_fill.execution.side == "BOT" else OrderSide.SELL,
                        price=ib_fill.execution.price,
                        quantity=abs(ib_fill.execution.shares),
                        commission=ib_fill.commissionReport.commission if ib_fill.commissionReport else 0,
                        timestamp=ib_fill.execution.time,
                    )
                    new_fills.append(fill)
                    self._fills.append(fill)

            # Track completed orders
            if trade.isDone():
                completed.append(our_id)

        for our_id in completed:
            self._pending_orders.pop(our_id, None)

        return new_fills

    async def get_positions(self) -> Dict[str, float]:
        """Get current positions from IB."""
        if not self._connected:
            return {}

        loop = asyncio.get_event_loop()
        positions = await loop.run_in_executor(None, lambda: self._ib.positions())

        result = {}
        for pos in positions:
            symbol = pos.contract.symbol
            result[symbol] = float(pos.position)

        return result

    async def get_account_summary(self) -> Dict[str, Any]:
        """Get account summary from IB."""
        if not self._connected:
            return {}

        loop = asyncio.get_event_loop()
        summary = await loop.run_in_executor(None, lambda: self._ib.accountSummary())

        result = {}
        for item in summary:
            result[item.tag] = item.value

        return {
            "net_liquidation": float(result.get("NetLiquidation", 0)),
            "equity": float(result.get("EquityWithLoanValue", 0)),
            "buying_power": float(result.get("BuyingPower", 0)),
            "cash": float(result.get("TotalCashValue", 0)),
            "unrealized_pnl": float(result.get("UnrealizedPnL", 0)),
            "realized_pnl": float(result.get("RealizedPnL", 0)),
        }

    async def health_check(self) -> bool:
        """Check if connected to IB."""
        if not self._ib:
            return False
        return self._ib.isConnected()

    # --- Options-specific methods ---

    async def submit_option_order(
        self,
        underlying: str,
        expiry: str,
        strike: float,
        right: str,  # "C" or "P"
        action: str,  # "BUY" or "SELL"
        quantity: int,
        order_type: str = "MKT",
        limit_price: Optional[float] = None,
    ) -> str:
        """Submit a single-leg option order.

        Args:
            underlying: e.g., "SPY"
            expiry: e.g., "20260320" (YYYYMMDD)
            strike: Strike price
            right: "C" for call, "P" for put
            action: "BUY" or "SELL"
            quantity: Number of contracts
            order_type: "MKT" or "LMT"
            limit_price: Required for limit orders

        Returns:
            Broker order ID
        """
        if not self._connected:
            raise ConnectionError("Not connected to IB")

        from ib_insync import Option, MarketOrder, LimitOrder

        contract = Option(underlying, expiry, strike, right, "SMART")

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: self._ib.qualifyContracts(contract)
        )

        if order_type == "LMT" and limit_price is not None:
            ib_order = LimitOrder(action, quantity, limit_price)
        else:
            ib_order = MarketOrder(action, quantity)

        trade = await loop.run_in_executor(
            None, lambda: self._ib.placeOrder(contract, ib_order)
        )

        broker_id = str(trade.order.orderId)
        logger.info(
            "ib_option_order_submitted",
            broker_id=broker_id,
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            right=right,
            action=action,
            qty=quantity,
        )
        return broker_id

    async def submit_straddle_order(
        self,
        underlying: str,
        expiry: str,
        strike: float,
        action: str,  # "BUY" or "SELL"
        quantity: int,
        order_type: str = "MKT",
    ) -> Dict[str, str]:
        """Submit a straddle (call + put at same strike).

        Returns dict with call and put order IDs.
        """
        call_id = await self.submit_option_order(
            underlying, expiry, strike, "C", action, quantity, order_type
        )
        put_id = await self.submit_option_order(
            underlying, expiry, strike, "P", action, quantity, order_type
        )
        return {"call_order_id": call_id, "put_order_id": put_id}

    async def get_option_greeks(
        self,
        underlying: str,
        expiry: str,
        strike: float,
        right: str,
    ) -> Dict[str, float]:
        """Get real-time option Greeks from IB."""
        if not self._connected:
            return {}

        from ib_insync import Option

        contract = Option(underlying, expiry, strike, right, "SMART")

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: self._ib.qualifyContracts(contract)
        )

        ticker = self._ib.reqMktData(contract, genericTickList="106")
        await asyncio.sleep(2)  # Wait for data

        greeks = ticker.modelGreeks or ticker.lastGreeks
        if greeks:
            return {
                "delta": greeks.delta or 0,
                "gamma": greeks.gamma or 0,
                "theta": greeks.theta or 0,
                "vega": greeks.vega or 0,
                "implied_vol": greeks.impliedVol or 0,
                "underlying_price": greeks.undPrice or 0,
            }
        return {}

    def __init__(self, host="127.0.0.1", port=7497, client_id=10, timeout=30):
        """Initialize IB execution engine."""
        self._host = host
        self._port = port
        self._client_id = client_id
        self._timeout = timeout
        self._ib = None
        self._connected = False
        self._pending_orders: Dict[str, Any] = {}
        self._fills: List[Fill] = []
        self._order_map: Dict[str, int] = {}
        self._processed_fills: set = set()
