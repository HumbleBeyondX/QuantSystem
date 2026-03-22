"""News data connector: fetches news articles for sentiment analysis."""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp

from core.enums import AssetClass
from core.logging import get_logger
from data.connectors.base import DataConnector

logger = get_logger(__name__)


class NewsConnector(DataConnector):
    """Fetches news from NewsAPI and Benzinga for NLP sentiment."""

    NEWSAPI_URL = "https://newsapi.org/v2"

    def __init__(
        self,
        newsapi_key: str = "",
        benzinga_key: str = "",
    ) -> None:
        self._newsapi_key = newsapi_key
        self._benzinga_key = benzinga_key
        self._session: Optional[aiohttp.ClientSession] = None
        self._running = False

    @property
    def name(self) -> str:
        return "news"

    @property
    def asset_class(self) -> AssetClass:
        return AssetClass.EQUITY

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        logger.info("news_connector_connected")

    async def disconnect(self) -> None:
        self._running = False
        if self._session:
            await self._session.close()

    async def subscribe(self, symbols: List[str], data_types: List[str]) -> None:
        self._running = True

    async def unsubscribe(self, symbols: List[str]) -> None:
        pass

    async def stream(self) -> AsyncIterator[Dict[str, Any]]:
        """News connectors don't stream — use fetch_articles instead."""
        return
        yield  # Make it a proper async generator

    async def fetch_articles(
        self,
        query: str,
        from_date: str = "",
        to_date: str = "",
        page_size: int = 20,
    ) -> List[Dict[str, Any]]:
        """Fetch news articles from NewsAPI."""
        if not self._session:
            self._session = aiohttp.ClientSession()

        params: Dict[str, Any] = {
            "q": query,
            "apiKey": self._newsapi_key,
            "pageSize": page_size,
            "language": "en",
            "sortBy": "publishedAt",
        }
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date

        try:
            async with self._session.get(
                f"{self.NEWSAPI_URL}/everything", params=params
            ) as resp:
                data = await resp.json()
                articles = data.get("articles", [])
                return [
                    {
                        "source": "newsapi",
                        "title": a.get("title", ""),
                        "description": a.get("description", ""),
                        "url": a.get("url", ""),
                        "published_at": a.get("publishedAt", ""),
                        "source_name": a.get("source", {}).get("name", ""),
                    }
                    for a in articles
                ]
        except Exception as e:
            logger.error("news_fetch_failed", error=str(e))
            return []

    async def get_historical(self, symbol: str, **kwargs: Any) -> List[Any]:
        return await self.fetch_articles(query=symbol)

    async def health_check(self) -> bool:
        return bool(self._newsapi_key)
