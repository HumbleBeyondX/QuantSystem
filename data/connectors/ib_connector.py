"""Interactive Brokers TWS/Gateway connector via ib_insync.

Provides real-time quotes, historical OHLCV, options chains, and Greeks
for US equities and options.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

from ib_insync import IB, Contract, Option, Stock, util

from core.enums import AssetClass, TimeFrame
from core.logging import get_logger
from core.models import Bar
from data.connectors.base import DataConnector

logger = get_logger(__name__)

# IB bar size mapping
TF_TO_IB = {
    TimeFrame.S1: "1 secs",
    TimeFrame.M1: "1 min",
    TimeFrame.M5: "5 mins",
    TimeFrame.M15: "15 mins",
    TimeFrame.M30: "30 mins",
    TimeFrame.H1: "1 hour",
    TimeFrame.H4: "4 hours",
    TimeFrame.D1: "1 day",
    TimeFrame.W1: "1 week",
}


class IBConnector(DataConnector):
    """Interactive Brokers TWS/Gateway connector.

    Connects via ib_insync for stocks, options, historical data, and Greeks.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 1,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._ib = IB()
        self._subscriptions: Dict[str, Contract] = {}
        self._running = False

    @property
    def name(self) -> str:
        return "interactive_brokers"

    @property
    def asset_class(self) -> AssetClass:
        return AssetClass.EQUITY

    async def connect(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self._ib.connect(
                self._host, self._port, clientId=self._client_id, timeout=30
            ),
        )
        logger.info("ib_connected", host=self._host, port=self._port)

    async def disconnect(self) -> None:
        self._running = False
        self._ib.disconnect()
        logger.info("ib_disconnected")

    async def subscribe(self, symbols: List[str], data_types: List[str]) -> None:
        self._running = True
        for sym in symbols:
            contract = Stock(sym, "SMART", "USD")
            self._ib.qualifyContracts(contract)
            self._subscriptions[sym] = contract

            if "tick" in data_types or "quote" in data_types:
                self._ib.reqMktData(contract, genericTickList="", snapshot=False)
            if "orderbook" in data_types:
                self._ib.reqMktDepth(contract, numRows=20)

        logger.info("ib_subscribed", symbols=symbols, data_types=data_types)

    async def unsubscribe(self, symbols: List[str]) -> None:
        for sym in symbols:
            contract = self._subscriptions.pop(sym, None)
            if contract:
                self._ib.cancelMktData(contract)

    async def stream(self) -> AsyncIterator[Dict[str, Any]]:
        """Yield normalized market data from IB callbacks."""
        while self._running and self._ib.isConnected():
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._ib.sleep(0.05)
            )

            for ticker in self._ib.tickers():
                if ticker.time:
                    yield {
                        "source": "ib",
                        "symbol": ticker.contract.symbol,
                        "data_type": "tick",
                        "bid": ticker.bid if ticker.bid != -1 else 0,
                        "ask": ticker.ask if ticker.ask != -1 else 0,
                        "last": ticker.last if ticker.last != -1 else 0,
                        "volume": ticker.volume if ticker.volume != -1 else 0,
                        "timestamp": ticker.time.isoformat() if ticker.time else "",
                    }

    async def get_historical(
        self, symbol: str, timeframe: TimeFrame, start: str, end: str
    ) -> List[Bar]:
        """Fetch historical bars from IB."""
        contract = Stock(symbol, "SMART", "USD")
        self._ib.qualifyContracts(contract)

        bar_size = TF_TO_IB.get(timeframe, "1 day")

        # Calculate duration from start/end
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
        days = (end_dt - start_dt).days
        duration = f"{days} D" if days > 0 else "1 D"

        bars = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._ib.reqHistoricalData(
                contract,
                endDateTime=end,
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=True,
            ),
        )

        return [
            Bar(
                symbol=symbol,
                timestamp=b.date if isinstance(b.date, datetime) else datetime.combine(b.date, datetime.min.time()),
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                timeframe=timeframe,
                source="ib",
                asset_class=AssetClass.EQUITY,
            )
            for b in bars
        ]

    async def get_options_chain(self, underlying: str) -> List[Dict[str, Any]]:
        """Fetch complete options chain for an underlying."""
        stock = Stock(underlying, "SMART", "USD")
        self._ib.qualifyContracts(stock)

        chains = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._ib.reqSecDefOptParams(
                stock.symbol, "", stock.secType, stock.conId
            ),
        )

        result = []
        for chain in chains:
            result.append({
                "exchange": chain.exchange,
                "underlying": underlying,
                "expirations": list(chain.expirations),
                "strikes": list(chain.strikes),
                "multiplier": chain.multiplier,
            })
        return result

    async def health_check(self) -> bool:
        return self._ib.isConnected()
