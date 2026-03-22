"""Polygon.io connector for US equity data (REST + WebSocket).

Provides millisecond-level tick data, NBBO quotes, and historical OHLCV.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp

from core.enums import AssetClass, TimeFrame
from core.logging import get_logger
from core.models import Bar
from data.connectors.base import DataConnector

logger = get_logger(__name__)

TF_TO_POLYGON = {
    TimeFrame.M1: (1, "minute"),
    TimeFrame.M5: (5, "minute"),
    TimeFrame.M15: (15, "minute"),
    TimeFrame.M30: (30, "minute"),
    TimeFrame.H1: (1, "hour"),
    TimeFrame.H4: (4, "hour"),
    TimeFrame.D1: (1, "day"),
    TimeFrame.W1: (1, "week"),
}


class PolygonConnector(DataConnector):
    """Polygon.io data connector for US equity market data."""

    BASE_URL = "https://api.polygon.io"
    WS_URL = "wss://socket.polygon.io"

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._symbols: List[str] = []
        self._data_types: List[str] = []
        self._running = False

    @property
    def name(self) -> str:
        return "polygon"

    @property
    def asset_class(self) -> AssetClass:
        return AssetClass.EQUITY

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        # WebSocket connection for real-time data
        self._ws = await self._session.ws_connect(f"{self.WS_URL}/stocks")
        # Authenticate
        await self._ws.send_json({"action": "auth", "params": self._api_key})
        auth_response = await self._ws.receive_json()
        logger.info("polygon_connected", auth=auth_response)

    async def disconnect(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()
        logger.info("polygon_disconnected")

    async def subscribe(self, symbols: List[str], data_types: List[str]) -> None:
        self._symbols = symbols
        self._data_types = data_types
        self._running = True

        channels = []
        for sym in symbols:
            if "trade" in data_types:
                channels.append(f"T.{sym}")
            if "quote" in data_types:
                channels.append(f"Q.{sym}")
            if "bar" in data_types:
                channels.append(f"AM.{sym}")

        if channels and self._ws:
            await self._ws.send_json({
                "action": "subscribe",
                "params": ",".join(channels),
            })
            logger.info("polygon_subscribed", channels=channels)

    async def unsubscribe(self, symbols: List[str]) -> None:
        self._symbols = [s for s in self._symbols if s not in symbols]

    async def stream(self) -> AsyncIterator[Dict[str, Any]]:
        """Yield normalized events from Polygon WebSocket."""
        while self._running and self._ws:
            try:
                msg = await asyncio.wait_for(self._ws.receive(), timeout=5.0)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    events = msg.json()
                    if isinstance(events, list):
                        for event in events:
                            normalized = self._normalize_event(event)
                            if normalized:
                                yield normalized
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    logger.warning("polygon_ws_closed")
                    break
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error("polygon_stream_error", error=str(e))
                await asyncio.sleep(1)

    def _normalize_event(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert Polygon WebSocket event to standard format."""
        ev_type = event.get("ev", "")

        if ev_type == "T":  # Trade
            return {
                "source": "polygon",
                "symbol": event.get("sym", ""),
                "data_type": "trade",
                "price": event.get("p", 0),
                "amount": event.get("s", 0),
                "side": "",
                "timestamp": str(event.get("t", "")),
            }
        elif ev_type == "Q":  # Quote
            return {
                "source": "polygon",
                "symbol": event.get("sym", ""),
                "data_type": "quote",
                "bid": event.get("bp", 0),
                "ask": event.get("ap", 0),
                "bid_size": event.get("bs", 0),
                "ask_size": event.get("as", 0),
                "timestamp": str(event.get("t", "")),
            }
        elif ev_type == "AM":  # Aggregate Minute
            return {
                "source": "polygon",
                "symbol": event.get("sym", ""),
                "data_type": "bar",
                "timeframe": "1m",
                "open": event.get("o", 0),
                "high": event.get("h", 0),
                "low": event.get("l", 0),
                "close": event.get("c", 0),
                "volume": event.get("v", 0),
                "vwap": event.get("vw", 0),
                "timestamp": str(event.get("s", "")),
            }
        return None

    async def get_historical(
        self, symbol: str, timeframe: TimeFrame, start: str, end: str
    ) -> List[Bar]:
        """Fetch historical bars via Polygon REST API."""
        if not self._session:
            self._session = aiohttp.ClientSession()

        multiplier, span = TF_TO_POLYGON.get(timeframe, (1, "day"))
        url = (
            f"{self.BASE_URL}/v2/aggs/ticker/{symbol}/range"
            f"/{multiplier}/{span}/{start}/{end}"
            f"?apiKey={self._api_key}&limit=50000&sort=asc"
        )

        async with self._session.get(url) as resp:
            data = await resp.json()
            results = data.get("results", [])

        from core.utils.time_utils import ms_to_datetime

        return [
            Bar(
                symbol=symbol,
                timestamp=ms_to_datetime(r["t"]),
                open=r["o"],
                high=r["h"],
                low=r["l"],
                close=r["c"],
                volume=r["v"],
                vwap=r.get("vw"),
                timeframe=timeframe,
                source="polygon",
                asset_class=AssetClass.EQUITY,
            )
            for r in results
        ]

    async def health_check(self) -> bool:
        try:
            if not self._session:
                return False
            async with self._session.get(
                f"{self.BASE_URL}/v3/reference/tickers?limit=1&apiKey={self._api_key}"
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
