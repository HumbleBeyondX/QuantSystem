"""DEX connector: dYdX and Hyperliquid perpetual futures."""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp

from core.enums import AssetClass
from core.logging import get_logger
from data.connectors.base import DataConnector

logger = get_logger(__name__)


class DEXConnector(DataConnector):
    """Connector for decentralized perpetual exchanges (dYdX, Hyperliquid)."""

    HYPERLIQUID_WS = "wss://api.hyperliquid.xyz/ws"
    HYPERLIQUID_REST = "https://api.hyperliquid.xyz"

    def __init__(self, exchange: str = "hyperliquid") -> None:
        self._exchange = exchange
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._symbols: List[str] = []
        self._running = False

    @property
    def name(self) -> str:
        return f"dex_{self._exchange}"

    @property
    def asset_class(self) -> AssetClass:
        return AssetClass.CRYPTO

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self.HYPERLIQUID_WS)
        logger.info("dex_connected", exchange=self._exchange)

    async def disconnect(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()
        logger.info("dex_disconnected", exchange=self._exchange)

    async def subscribe(self, symbols: List[str], data_types: List[str]) -> None:
        self._symbols = symbols
        self._running = True

        for sym in symbols:
            if "trade" in data_types:
                await self._ws_subscribe({"type": "trades", "coin": sym})
            if "orderbook" in data_types or "l2Book" in data_types:
                await self._ws_subscribe({"type": "l2Book", "coin": sym})

    async def _ws_subscribe(self, subscription: Dict[str, Any]) -> None:
        if self._ws:
            await self._ws.send_json({
                "method": "subscribe",
                "subscription": subscription,
            })
            logger.info("dex_subscribed", sub=subscription)

    async def unsubscribe(self, symbols: List[str]) -> None:
        self._symbols = [s for s in self._symbols if s not in symbols]

    async def stream(self) -> AsyncIterator[Dict[str, Any]]:
        """Yield normalized events from DEX WebSocket."""
        while self._running and self._ws:
            try:
                msg = await asyncio.wait_for(self._ws.receive(), timeout=5.0)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    data = msg.json()
                    normalized = self._normalize(data)
                    if normalized:
                        yield normalized
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    logger.warning("dex_ws_closed")
                    break
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error("dex_stream_error", error=str(e))
                await asyncio.sleep(1)

    def _normalize(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize Hyperliquid WebSocket data."""
        channel = data.get("channel", "")

        if channel == "trades":
            trades = data.get("data", [])
            if trades:
                t = trades[0]
                return {
                    "source": self._exchange,
                    "symbol": t.get("coin", ""),
                    "data_type": "trade",
                    "price": float(t.get("px", 0)),
                    "amount": float(t.get("sz", 0)),
                    "side": t.get("side", "").upper(),
                    "timestamp": t.get("time", ""),
                }
        elif channel == "l2Book":
            book = data.get("data", {})
            return {
                "source": self._exchange,
                "symbol": book.get("coin", ""),
                "data_type": "orderbook",
                "bids": [
                    [float(lvl.get("px", 0)), float(lvl.get("sz", 0))]
                    for lvl in book.get("levels", [[]])[0]
                ],
                "asks": [
                    [float(lvl.get("px", 0)), float(lvl.get("sz", 0))]
                    for lvl in book.get("levels", [[], []])[1]
                ],
                "timestamp": str(book.get("time", "")),
            }
        return None

    async def get_historical(self, symbol: str, **kwargs: Any) -> List[Any]:
        """Fetch historical data via REST (limited for DEXes)."""
        return []

    async def health_check(self) -> bool:
        try:
            if not self._session:
                return False
            async with self._session.get(
                f"{self.HYPERLIQUID_REST}/info",
                json={"type": "meta"},
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
