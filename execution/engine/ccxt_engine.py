"""CCXT Pro live execution engine for crypto exchanges.

Supports Binance, Bybit, and OKX perpetual futures and spot markets.
Exchange-agnostic via CCXT Pro — one engine class handles all three.

SAFETY:
  - Withdrawal operations are explicitly BLOCKED at the engine level.
  - Even if the API key has withdrawal permissions, this engine refuses.

Usage:
    engine = CCXTExecutionEngine(
        exchange_id="binance",
        api_key="...",
        api_secret="...",
        sandbox=True,
    )
    await engine.connect()
    broker_id = await engine.submit_order(order)
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

# Exchanges we officially support
SUPPORTED_EXCHANGES = {"binance", "bybit", "okx"}


# ------------------------------------------------------------------
# Exchange-specific parameter adapters
# ------------------------------------------------------------------

def _binance_order_params(
    symbol: str,
    order_type: str,
    leverage: Optional[int],
    margin_type: Optional[str],
    stop_price: Optional[float],
    reduce_only: bool,
) -> Dict[str, Any]:
    """Binance-specific order params."""
    params: Dict[str, Any] = {}
    if reduce_only:
        params["reduceOnly"] = True
    if stop_price is not None:
        params["stopPrice"] = stop_price
        params["type"] = "STOP_MARKET" if order_type == "MARKET" else "STOP"
    return params


def _bybit_order_params(
    symbol: str,
    order_type: str,
    leverage: Optional[int],
    margin_type: Optional[str],
    stop_price: Optional[float],
    reduce_only: bool,
) -> Dict[str, Any]:
    """Bybit-specific order params."""
    params: Dict[str, Any] = {"category": "linear"}
    if reduce_only:
        params["reduceOnly"] = True
    if stop_price is not None:
        params["triggerPrice"] = str(stop_price)
        params["orderType"] = "Market" if order_type == "MARKET" else "Limit"
    return params


def _okx_order_params(
    symbol: str,
    order_type: str,
    leverage: Optional[int],
    margin_type: Optional[str],
    stop_price: Optional[float],
    reduce_only: bool,
) -> Dict[str, Any]:
    """OKX-specific order params."""
    params: Dict[str, Any] = {"instType": "SWAP"}
    if margin_type:
        params["tdMode"] = "cross" if margin_type == "cross" else "isolated"
    else:
        params["tdMode"] = "cross"
    # posSide: "net" for one-way position mode (default for our system).
    # This must match the account's position mode — we set net mode on connect.
    params["posSide"] = "net"
    if reduce_only:
        params["reduceOnly"] = True
    if stop_price is not None:
        params["triggerPx"] = str(stop_price)
        params["ordType"] = "trigger"
    return params


_EXCHANGE_PARAM_ADAPTERS = {
    "binance": _binance_order_params,
    "bybit": _bybit_order_params,
    "okx": _okx_order_params,
}


class CCXTExecutionEngine(ExecutionEngine):
    """Live execution engine for crypto exchanges via CCXT Pro.

    Supports perpetual futures and spot trading on Binance, Bybit, and OKX.
    All withdrawal operations are explicitly blocked for safety.
    """

    def __init__(
        self,
        exchange_id: str = "binance",
        api_key: str = "",
        api_secret: str = "",
        passphrase: str = "",  # OKX requires a passphrase
        sandbox: bool = True,
        default_leverage: int = 1,
        default_margin_type: str = "cross",  # cross / isolated
    ) -> None:
        if exchange_id not in SUPPORTED_EXCHANGES:
            raise ValueError(
                f"Unsupported exchange: {exchange_id}. "
                f"Supported: {SUPPORTED_EXCHANGES}"
            )
        self._exchange_id = exchange_id
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        self._sandbox = sandbox
        self._default_leverage = default_leverage
        self._default_margin_type = default_margin_type

        self._exchange: Any = None  # ccxt pro exchange instance
        self._connected = False
        self._pending_orders: Dict[str, Dict[str, Any]] = {}  # our_id -> order info
        self._fills: List[Fill] = []
        self._processed_fills: set = set()
        self._ws_task: Optional[asyncio.Task] = None
        self._leverage_set: set = set()  # symbols where leverage was already configured

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to the exchange via CCXT Pro."""
        try:
            import ccxt.pro as ccxtpro
        except ImportError:
            raise ImportError(
                "ccxt[pro] not installed. Run: pip install ccxt"
            )

        exchange_class = getattr(ccxtpro, self._exchange_id, None)
        if exchange_class is None:
            raise ValueError(f"CCXT Pro does not support exchange: {self._exchange_id}")

        config: Dict[str, Any] = {
            "apiKey": self._api_key,
            "secret": self._api_secret,
            "enableRateLimit": True,
            "timeout": 30000,
            "options": {
                "defaultType": "swap",  # perpetual futures by default
            },
        }
        if self._passphrase:
            config["password"] = self._passphrase

        self._exchange = exchange_class(config)

        if self._sandbox:
            self._exchange.set_sandbox_mode(True)

        # Fix: Use ThreadedResolver to avoid Windows aiohttp async DNS bug
        try:
            import aiohttp
            resolver = aiohttp.resolver.ThreadedResolver()
            connector = aiohttp.TCPConnector(resolver=resolver)
            self._exchange.session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=30),
            )
        except Exception as e:
            logger.warning("threaded_resolver_setup_skipped", error=str(e))

        # Verify connectivity by loading markets
        await self._exchange.load_markets()

        # OKX: Set account to net position mode (one-way) for simpler order management.
        # In net mode, no posSide param needed (buy to open long, sell to open short).
        # Must be done before placing any orders and when no positions are open.
        if self._exchange_id == "okx":
            try:
                await self._exchange.set_position_mode(hedged=False)
                logger.info("okx_position_mode_set", mode="net_mode")
            except Exception as e:
                # May fail if already in net mode or positions are open
                logger.warning("okx_position_mode_unchanged", reason=str(e))

        self._connected = True

        logger.info(
            "ccxt_engine_connected",
            exchange=self._exchange_id,
            sandbox=self._sandbox,
            markets=len(self._exchange.markets),
        )

    async def disconnect(self) -> None:
        """Disconnect and clean up."""
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._exchange:
            try:
                await self._exchange.close()
            except Exception:
                pass

        self._connected = False
        logger.info("ccxt_engine_disconnected", exchange=self._exchange_id)

    # ------------------------------------------------------------------
    # Withdrawal BLOCK
    # ------------------------------------------------------------------

    async def withdraw(self, *args: Any, **kwargs: Any) -> None:
        """BLOCKED — withdrawal operations are not permitted."""
        logger.critical(
            "WITHDRAWAL_BLOCKED",
            exchange=self._exchange_id,
            message="Withdrawal attempt was blocked by engine safety policy",
        )
        raise PermissionError(
            "Withdrawal operations are explicitly BLOCKED. "
            "This engine is for trading only — no fund transfers allowed."
        )

    # ------------------------------------------------------------------
    # Leverage & margin configuration
    # ------------------------------------------------------------------

    async def _ensure_leverage(
        self,
        symbol: str,
        leverage: Optional[int] = None,
        margin_type: Optional[str] = None,
    ) -> None:
        """Set leverage and margin type for a symbol (idempotent)."""
        lev = leverage or self._default_leverage
        mt = margin_type or self._default_margin_type
        cache_key = f"{symbol}_{lev}_{mt}"

        if cache_key in self._leverage_set:
            return

        try:
            # Set margin type (cross/isolated) — not all exchanges support this
            if hasattr(self._exchange, "set_margin_mode"):
                await self._exchange.set_margin_mode(mt, symbol)
        except Exception as e:
            # Some exchanges don't support setting margin mode via API
            logger.debug("set_margin_mode_skipped", symbol=symbol, reason=str(e))

        try:
            await self._exchange.set_leverage(lev, symbol)
            self._leverage_set.add(cache_key)
            logger.info(
                "leverage_configured",
                symbol=symbol,
                leverage=lev,
                margin_type=mt,
            )
        except Exception as e:
            logger.warning("set_leverage_failed", symbol=symbol, error=str(e))

    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------

    async def submit_order(self, order: Order) -> str:
        """Submit an order to the exchange.

        Handles market, limit, stop orders with exchange-specific param mapping.
        Automatically configures leverage before first order on a symbol.

        Returns:
            Broker order ID (exchange order ID as string)
        """
        if not self._connected:
            raise ConnectionError(f"Not connected to {self._exchange_id}")

        symbol = order.symbol
        side = "buy" if order.side == OrderSide.BUY else "sell"
        amount = abs(order.quantity)
        price = order.limit_price

        # Determine CCXT order type
        if order.order_type == OrderType.MARKET:
            ccxt_type = "market"
            price = None
        elif order.order_type == OrderType.LIMIT:
            ccxt_type = "limit"
            if price is None:
                raise ValueError("Limit order requires limit_price")
        elif order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            ccxt_type = "limit" if order.limit_price else "market"
        else:
            ccxt_type = "market"

        # Configure leverage
        leverage = order.metadata.get("leverage", self._default_leverage)
        margin_type = order.metadata.get("margin_type", self._default_margin_type)
        await self._ensure_leverage(symbol, leverage, margin_type)

        # Build exchange-specific params
        adapter = _EXCHANGE_PARAM_ADAPTERS.get(self._exchange_id, _binance_order_params)
        reduce_only = order.metadata.get("reduce_only", False)
        params = adapter(
            symbol=symbol,
            order_type=order.order_type.value if isinstance(order.order_type, OrderType) else str(order.order_type),
            leverage=leverage,
            margin_type=margin_type,
            stop_price=order.stop_price,
            reduce_only=reduce_only,
        )

        try:
            result = await self._exchange.create_order(
                symbol=symbol,
                type=ccxt_type,
                side=side,
                amount=amount,
                price=price,
                params=params,
            )
        except Exception as e:
            logger.error(
                "ccxt_order_failed",
                exchange=self._exchange_id,
                symbol=symbol,
                side=side,
                amount=amount,
                error=str(e),
            )
            raise

        broker_id = str(result.get("id", ""))
        our_id = str(order.order_id)

        self._pending_orders[our_id] = {
            "broker_id": broker_id,
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "result": result,
            "strategy_id": order.strategy_id,
        }

        logger.info(
            "ccxt_order_submitted",
            exchange=self._exchange_id,
            our_id=our_id,
            broker_id=broker_id,
            symbol=symbol,
            side=side,
            amount=amount,
            type=ccxt_type,
            leverage=leverage,
        )

        # If the order was already filled (market order), process immediately
        status = result.get("status", "")
        if status == "closed":
            self._process_filled_order(our_id, result)

        return broker_id

    def _process_filled_order(self, our_id: str, result: Dict[str, Any]) -> None:
        """Extract fill from a completed order result."""
        fill_key = f"{our_id}_{result.get('id', '')}"
        if fill_key in self._processed_fills:
            return
        self._processed_fills.add(fill_key)

        fill = Fill(
            fill_id=uuid4(),
            order_id=our_id,
            symbol=result.get("symbol", ""),
            side=OrderSide.BUY if result.get("side") == "buy" else OrderSide.SELL,
            price=float(result.get("average", 0) or result.get("price", 0) or 0),
            quantity=float(result.get("filled", 0) or result.get("amount", 0)),
            commission=float(result.get("fee", {}).get("cost", 0) if result.get("fee") else 0),
            timestamp=datetime.now(timezone.utc),
            strategy_id=self._pending_orders.get(our_id, {}).get("strategy_id", ""),
            asset_class=AssetClass.CRYPTO_PERP,
        )
        self._fills.append(fill)

    # ------------------------------------------------------------------
    # Order cancellation
    # ------------------------------------------------------------------

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a pending order by our internal order_id."""
        if not self._connected:
            return False

        info = self._pending_orders.get(order_id)
        if not info:
            logger.warning("ccxt_cancel_unknown_order", order_id=order_id)
            return False

        try:
            await self._exchange.cancel_order(info["broker_id"], info["symbol"])
            self._pending_orders.pop(order_id, None)
            logger.info("ccxt_order_cancelled", order_id=order_id, broker_id=info["broker_id"])
            return True
        except Exception as e:
            logger.error("ccxt_cancel_failed", order_id=order_id, error=str(e))
            return False

    # ------------------------------------------------------------------
    # Fills
    # ------------------------------------------------------------------

    async def get_fills(self) -> List[Fill]:
        """Return and clear buffered fills."""
        fills = list(self._fills)
        self._fills.clear()
        return fills

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    async def get_positions(self) -> Dict[str, float]:
        """Get current open positions from the exchange.

        Returns:
            Dict of symbol -> signed quantity (positive=long, negative=short)
        """
        if not self._connected:
            return {}

        try:
            positions = await self._exchange.fetch_positions()
        except Exception as e:
            logger.error("ccxt_fetch_positions_failed", error=str(e))
            return {}

        result: Dict[str, float] = {}
        for pos in positions:
            contracts = float(pos.get("contracts", 0) or 0)
            if contracts == 0:
                continue
            side = pos.get("side", "")
            qty = contracts if side == "long" else -contracts
            symbol = pos.get("symbol", "")
            result[symbol] = qty

        return result

    async def get_positions_detailed(self) -> List[Dict[str, Any]]:
        """Get detailed position info including PnL, leverage, liquidation price."""
        if not self._connected:
            return []

        try:
            positions = await self._exchange.fetch_positions()
        except Exception as e:
            logger.error("ccxt_fetch_positions_detail_failed", error=str(e))
            return []

        result = []
        for pos in positions:
            contracts = float(pos.get("contracts", 0) or 0)
            if contracts == 0:
                continue
            result.append({
                "symbol": pos.get("symbol", ""),
                "side": pos.get("side", ""),
                "contracts": contracts,
                "entry_price": float(pos.get("entryPrice", 0) or 0),
                "mark_price": float(pos.get("markPrice", 0) or 0),
                "liquidation_price": float(pos.get("liquidationPrice", 0) or 0),
                "unrealized_pnl": float(pos.get("unrealizedPnl", 0) or 0),
                "leverage": float(pos.get("leverage", 0) or 0),
                "margin_type": pos.get("marginMode", ""),
                "notional": float(pos.get("notional", 0) or 0),
            })
        return result

    # ------------------------------------------------------------------
    # Balances
    # ------------------------------------------------------------------

    async def get_balances(self) -> Dict[str, Any]:
        """Get wallet balances from the exchange.

        Returns:
            Dict with total, free, used balances and margin info.
        """
        if not self._connected:
            return {}

        try:
            balance = await self._exchange.fetch_balance({"type": "swap"})
        except Exception as e:
            logger.error("ccxt_fetch_balance_failed", error=str(e))
            return {}

        usdt = balance.get("USDT", {})
        return {
            "total": float(usdt.get("total", 0) or 0),
            "free": float(usdt.get("free", 0) or 0),
            "used": float(usdt.get("used", 0) or 0),
            "currency": "USDT",
            "exchange": self._exchange_id,
        }

    # ------------------------------------------------------------------
    # WebSocket monitoring
    # ------------------------------------------------------------------

    async def start_ws_monitor(self, symbols: Optional[List[str]] = None) -> None:
        """Start WebSocket streams for order/position updates.

        Runs in background, processing order updates and converting them to fills.
        """
        if self._ws_task and not self._ws_task.done():
            return

        self._ws_task = asyncio.create_task(self._ws_monitor_loop(symbols or []))
        logger.info("ccxt_ws_monitor_started", exchange=self._exchange_id)

    async def _ws_monitor_loop(self, symbols: List[str]) -> None:
        """Background loop watching order updates via WebSocket."""
        while self._connected:
            try:
                # Watch for order updates (supported by all 3 exchanges)
                orders = await self._exchange.watch_orders()
                for order_update in orders:
                    self._handle_order_update(order_update)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("ccxt_ws_error", error=str(e))
                await asyncio.sleep(1)

    def _handle_order_update(self, order_data: Dict[str, Any]) -> None:
        """Process an order update from WebSocket."""
        broker_id = str(order_data.get("id", ""))
        status = order_data.get("status", "")

        # Find our internal order ID
        our_id = None
        for oid, info in self._pending_orders.items():
            if info.get("broker_id") == broker_id:
                our_id = oid
                break

        if not our_id:
            return

        if status in ("closed", "filled"):
            self._process_filled_order(our_id, order_data)
            self._pending_orders.pop(our_id, None)
        elif status in ("canceled", "cancelled", "expired", "rejected"):
            self._pending_orders.pop(our_id, None)
            logger.info(
                "ccxt_order_terminal",
                our_id=our_id,
                broker_id=broker_id,
                status=status,
            )

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Check exchange connectivity."""
        if not self._connected or not self._exchange:
            return False
        try:
            await self._exchange.fetch_time()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Market info & precision
    # ------------------------------------------------------------------

    def get_market_info(self, symbol: str) -> Dict[str, Any]:
        """Get exchange market info for a symbol: minimums, precision, etc.

        Returns:
            Dict with keys:
              - min_amount: Minimum order qty (e.g. 0.01 BTC)
              - min_cost: Minimum notional value (e.g. $5)
              - amount_precision: Decimal places for quantity
              - price_precision: Decimal places for price
              - contract_size: Contract multiplier (usually 1 for linear perps)
        """
        if not self._exchange or not self._exchange.markets:
            return {}

        market = self._exchange.market(symbol) if symbol in self._exchange.markets else None
        if not market:
            logger.warning("market_not_found", symbol=symbol)
            return {}

        limits = market.get("limits", {})
        precision = market.get("precision", {})

        return {
            "min_amount": float(limits.get("amount", {}).get("min", 0) or 0),
            "max_amount": float(limits.get("amount", {}).get("max", 0) or 0),
            "min_cost": float(limits.get("cost", {}).get("min", 0) or 0),
            "min_price": float(limits.get("price", {}).get("min", 0) or 0),
            "amount_precision": precision.get("amount"),
            "price_precision": precision.get("price"),
            "contract_size": float(market.get("contractSize", 1) or 1),
        }

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        """Round an order amount to the exchange's required precision.

        Uses CCXT's built-in precision handling which accounts for
        step sizes, decimal places, and significant figures.
        """
        if not self._exchange:
            return amount
        try:
            return float(self._exchange.amount_to_precision(symbol, amount))
        except Exception as e:
            logger.warning("amount_precision_fallback", symbol=symbol, error=str(e))
            return amount

    def price_to_precision(self, symbol: str, price: float) -> float:
        """Round a price to the exchange's required precision."""
        if not self._exchange:
            return price
        try:
            return float(self._exchange.price_to_precision(symbol, price))
        except Exception as e:
            logger.warning("price_precision_fallback", symbol=symbol, error=str(e))
            return price

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    async def fetch_ticker(self, symbol: str) -> Dict[str, float]:
        """Fetch current ticker for a symbol (utility for strategies)."""
        if not self._connected:
            return {}
        try:
            ticker = await self._exchange.fetch_ticker(symbol)
            return {
                "last": float(ticker.get("last", 0) or 0),
                "bid": float(ticker.get("bid", 0) or 0),
                "ask": float(ticker.get("ask", 0) or 0),
                "volume": float(ticker.get("baseVolume", 0) or 0),
            }
        except Exception as e:
            logger.error("ccxt_fetch_ticker_failed", symbol=symbol, error=str(e))
            return {}

    @property
    def exchange_id(self) -> str:
        return self._exchange_id

    @property
    def is_sandbox(self) -> bool:
        return self._sandbox

    @property
    def is_connected(self) -> bool:
        return self._connected
