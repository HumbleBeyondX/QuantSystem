"""Abstract base class for all data source connectors.

Every data source (IB, Polygon, CCXT, DEX, etc.) implements this interface.
This ensures consistent behavior across all connectors and allows the
IngestManager to treat them uniformly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Dict, List

from core.enums import AssetClass, TimeFrame
from core.models import Bar


class DataConnector(ABC):
    """Abstract base class for data source connectors.

    Lifecycle: connect() -> subscribe() -> stream() -> unsubscribe() -> disconnect()
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this connector (e.g., 'interactive_brokers', 'ccxt_binance')."""
        ...

    @property
    @abstractmethod
    def asset_class(self) -> AssetClass:
        """Primary asset class this connector serves."""
        ...

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the data source."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully close the connection."""
        ...

    @abstractmethod
    async def subscribe(self, symbols: List[str], data_types: List[str]) -> None:
        """Subscribe to real-time data for given symbols and data types.

        Args:
            symbols: List of trading symbols (e.g., ["AAPL", "BTC/USDT"])
            data_types: List of data types (e.g., ["tick", "orderbook", "trade"])
        """
        ...

    @abstractmethod
    async def unsubscribe(self, symbols: List[str]) -> None:
        """Unsubscribe from symbols."""
        ...

    @abstractmethod
    async def stream(self) -> AsyncIterator[Dict[str, Any]]:
        """Yield normalized market data events.

        Each yielded dict must contain at minimum:
            - source: str (connector name)
            - symbol: str
            - data_type: str (tick|trade|bar|orderbook)
            - timestamp: str (ISO 8601)
        """
        ...

    @abstractmethod
    async def get_historical(
        self,
        symbol: str,
        timeframe: TimeFrame,
        start: str,
        end: str,
    ) -> List[Bar]:
        """Fetch historical OHLCV bars.

        Args:
            symbol: Trading symbol
            timeframe: Bar timeframe
            start: Start date (ISO format or exchange-specific)
            end: End date
        """
        ...

    async def health_check(self) -> bool:
        """Check if the connector is healthy and connected.

        Returns True if operational, False otherwise.
        """
        return True
