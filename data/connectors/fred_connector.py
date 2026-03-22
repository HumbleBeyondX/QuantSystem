"""FRED connector: fetches macroeconomic data from the Federal Reserve."""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp
import pandas as pd

from core.enums import AssetClass
from core.logging import get_logger
from data.connectors.base import DataConnector

logger = get_logger(__name__)


class FREDConnector(DataConnector):
    """Federal Reserve Economic Data (FRED) connector for macro indicators."""

    BASE_URL = "https://api.stlouisfed.org/fred"

    # Common series IDs
    SERIES = {
        "fed_funds": "FEDFUNDS",
        "cpi": "CPIAUCSL",
        "unemployment": "UNRATE",
        "gdp": "GDP",
        "treasury_10y": "DGS10",
        "treasury_2y": "DGS2",
        "vix": "VIXCLS",
        "sp500": "SP500",
    }

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def name(self) -> str:
        return "fred"

    @property
    def asset_class(self) -> AssetClass:
        return AssetClass.EQUITY

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        logger.info("fred_connector_connected")

    async def disconnect(self) -> None:
        if self._session:
            await self._session.close()

    async def subscribe(self, symbols: List[str], data_types: List[str]) -> None:
        pass  # FRED is pull-based

    async def unsubscribe(self, symbols: List[str]) -> None:
        pass

    async def stream(self) -> AsyncIterator[Dict[str, Any]]:
        return
        yield

    async def fetch_series(
        self,
        series_id: str,
        start_date: str = "",
        end_date: str = "",
    ) -> pd.DataFrame:
        """Fetch a FRED time series as a DataFrame."""
        if not self._session:
            self._session = aiohttp.ClientSession()

        params: Dict[str, Any] = {
            "series_id": series_id,
            "api_key": self._api_key,
            "file_type": "json",
        }
        if start_date:
            params["observation_start"] = start_date
        if end_date:
            params["observation_end"] = end_date

        try:
            async with self._session.get(
                f"{self.BASE_URL}/series/observations", params=params
            ) as resp:
                data = await resp.json()
                observations = data.get("observations", [])

            records = []
            for obs in observations:
                try:
                    records.append({
                        "date": obs["date"],
                        "value": float(obs["value"]),
                    })
                except (ValueError, KeyError):
                    continue

            df = pd.DataFrame(records)
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"])
                df.set_index("date", inplace=True)
            return df

        except Exception as e:
            logger.error("fred_fetch_failed", series_id=series_id, error=str(e))
            return pd.DataFrame()

    async def fetch_multiple(
        self, series_ids: List[str], start_date: str = ""
    ) -> Dict[str, pd.DataFrame]:
        """Fetch multiple series concurrently."""
        import asyncio

        tasks = {
            sid: self.fetch_series(sid, start_date=start_date)
            for sid in series_ids
        }
        results = await asyncio.gather(*tasks.values())
        return dict(zip(tasks.keys(), results))

    async def get_historical(self, symbol: str, **kwargs: Any) -> List[Any]:
        series_id = self.SERIES.get(symbol, symbol)
        df = await self.fetch_series(series_id)
        return df.to_dict("records") if not df.empty else []

    async def health_check(self) -> bool:
        return bool(self._api_key)
