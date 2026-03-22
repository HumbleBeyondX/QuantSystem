"""CCXT Pro connector for crypto exchange data via WebSocket.

Supports Binance, OKX, Bybit and 130+ other exchanges.
Uses CCXT Pro (premium async WebSocket) for real-time data streaming.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Dict, List, Optional

import ccxt.pro as ccxtpro

from core.enums import AssetClass, TimeFrame
from core.logging import get_logger
from core.models import Bar
from data.connectors.base import DataConnector

logger = get_logger(__name__)

# Timeframe mapping: internal -> CCXT
TF_MAP = {
    TimeFrame.M1: "1m",
    TimeFrame.M5: "5m",
    TimeFrame.M15: "15m",
    TimeFrame.M30: "30m",
    TimeFrame.H1: "1h",
    TimeFrame.H4: "4h",
    TimeFrame.D1: "1d",
    TimeFrame.W1: "1w",
}


class CCXTConnector(DataConnector):
    """Crypto exchange connector via CCXT Pro WebSocket.

    Streams real-time trades, orderbook, and OHLCV data.
    """

    def __init__(
        self,
        exchange_id: str = "binance",
        api_key: str = "",
        api_secret: str = "",
        sandbox: bool = False,
    ) -> None:
        self._exchange_id = exchange_id
        self._api_key = api_key
        self._api_secret = api_secret
        self._sandbox = sandbox
        self._exchange: Optional[ccxtpro.Exchange] = None
        self._symbols: List[str] = []
        self._data_types: List[str] = []
        self._running = False

    @property
    def name(self) -> str:
        return f"ccxt_{self._exchange_id}"

    @property
    def asset_class(self) -> AssetClass:
        return AssetClass.CRYPTO

    async def connect(self) -> None:
        exchange_class = getattr(ccxtpro, self._exchange_id)
        config: Dict[str, Any] = {
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        }
        if self._api_key:
            config["apiKey"] = self._api_key
            config["secret"] = self._api_secret
        if self._sandbox:
            config["sandbox"] = True

        self._exchange = exchange_class(config)
        logger.info("ccxt_connected", exchange=self._exchange_id)

    async def disconnect(self) -> None:
        self._running = False
        if self._exchange:
            await self._exchange.close()
        logger.info("ccxt_disconnected", exchange=self._exchange_id)

    async def subscribe(self, symbols: List[str], data_types: List[str]) -> None:
        self._symbols = symbols
        self._data_types = data_types
        self._running = True

    async def unsubscribe(self, symbols: List[str]) -> None:
        self._symbols = [s for s in self._symbols if s not in symbols]

    async def stream(self) -> AsyncIterator[Dict[str, Any]]:
        """Stream real-time data from the exchange.

        Launches concurrent watch tasks for each data type and merges
        them into a single async stream via an asyncio.Queue.
        """
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=10000)
        tasks: List[asyncio.Task] = []

        if "trade" in self._data_types:
            tasks.append(asyncio.create_task(self._watch_trades(queue)))
        if "orderbook" in self._data_types:
            tasks.append(asyncio.create_task(self._watch_orderbooks(queue)))
        if "ohlcv" in self._data_types:
            tasks.append(asyncio.create_task(self._watch_ohlcv(queue)))

        try:
            while self._running:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                    yield event
                except asyncio.TimeoutError:
                    continue
        finally:
            for task in tasks:
                task.cancel()

    async def _watch_trades(self, queue: asyncio.Queue) -> None:
        """Watch trades for all subscribed symbols."""
        while self._running:
            for symbol in self._symbols:
                try:
                    trades = await self._exchange.watch_trades(symbol)
                    for t in trades:
                        await queue.put({
                            "source": self._exchange_id,
                            "symbol": symbol,
                            "data_type": "trade",
                            "price": t["price"],
                            "amount": t["amount"],
                            "side": t["side"].upper(),
                            "trade_id": str(t.get("id", "")),
                            "timestamp": t["datetime"],
                        })
                except Exception as e:
                    logger.error("ccxt_watch_trades_error", symbol=symbol, error=str(e))
                    await asyncio.sleep(1)

    async def _watch_orderbooks(self, queue: asyncio.Queue) -> None:
        """Watch orderbooks for all subscribed symbols."""
        while self._running:
            for symbol in self._symbols:
                try:
                    ob = await self._exchange.watch_order_book(symbol, limit=20)
                    await queue.put({
                        "source": self._exchange_id,
                        "symbol": symbol,
                        "data_type": "orderbook",
                        "bids": ob["bids"][:20],
                        "asks": ob["asks"][:20],
                        "timestamp": ob.get("datetime", ""),
                    })
                except Exception as e:
                    logger.error("ccxt_watch_ob_error", symbol=symbol, error=str(e))
                    await asyncio.sleep(1)

    async def _watch_ohlcv(self, queue: asyncio.Queue) -> None:
        """Watch OHLCV candles."""
        while self._running:
            for symbol in self._symbols:
                try:
                    candles = await self._exchange.watch_ohlcv(symbol, "1m")
                    for c in candles:
                        await queue.put({
                            "source": self._exchange_id,
                            "symbol": symbol,
                            "data_type": "ohlcv",
                            "timeframe": "1m",
                            "timestamp": c[0],
                            "open": c[1],
                            "high": c[2],
                            "low": c[3],
                            "close": c[4],
                            "volume": c[5],
                        })
                except Exception as e:
                    logger.error("ccxt_watch_ohlcv_error", symbol=symbol, error=str(e))
                    await asyncio.sleep(1)

    async def get_historical(
        self, symbol: str, timeframe: TimeFrame, start: str, end: str
    ) -> List[Bar]:
        """Fetch historical OHLCV using REST API."""
        from core.utils.time_utils import ms_to_datetime

        tf = TF_MAP.get(timeframe, "1m")
        since = int(
            __import__("datetime").datetime.fromisoformat(start).timestamp() * 1000
        )
        end_ms = int(
            __import__("datetime").datetime.fromisoformat(end).timestamp() * 1000
        )

        all_bars: List[Bar] = []
        while since < end_ms:
            ohlcv = await self._exchange.fetch_ohlcv(
                symbol, tf, since=since, limit=1000
            )
            if not ohlcv:
                break
            for c in ohlcv:
                all_bars.append(
                    Bar(
                        symbol=symbol,
                        timestamp=ms_to_datetime(c[0]),
                        open=c[1],
                        high=c[2],
                        low=c[3],
                        close=c[4],
                        volume=c[5],
                        timeframe=timeframe,
                        source=self._exchange_id,
                        asset_class=AssetClass.CRYPTO,
                    )
                )
            since = ohlcv[-1][0] + 1

        return all_bars

    async def health_check(self) -> bool:
        try:
            await self._exchange.fetch_time()
            return True
        except Exception:
            return False
