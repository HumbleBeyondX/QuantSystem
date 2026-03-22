"""Ingestion manager: orchestrates data flow from connectors to storage.

Flow: Connector.stream() -> Validate -> Normalize -> Kafka Produce -> Redis Cache
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from core.config import settings
from core.logging import get_logger
from data.connectors.base import DataConnector
from data.pipeline.normalizer import DataNormalizer
from data.pipeline.validator import DataValidator
from data.storage.redis_cache import RedisCache
from data.streaming.producer import KafkaProducerWrapper

logger = get_logger(__name__)


class IngestManager:
    """Orchestrates data ingestion from all connectors.

    Manages connector lifecycle, validates data quality, publishes to Kafka,
    and maintains Redis hot cache for real-time data.
    """

    def __init__(
        self,
        connectors: List[DataConnector],
        kafka_producer: KafkaProducerWrapper,
        redis_cache: RedisCache,
        validator: Optional[DataValidator] = None,
    ) -> None:
        self._connectors: Dict[str, DataConnector] = {c.name: c for c in connectors}
        self._producer = kafka_producer
        self._cache = redis_cache
        self._validator = validator or DataValidator()
        self._normalizer = DataNormalizer()
        self._tasks: List[asyncio.Task] = []
        self._running = False
        self._stats: Dict[str, Dict[str, int]] = {}

    async def start(self) -> None:
        """Start all connector ingestion loops."""
        self._running = True
        logger.info("ingest_manager_starting", connectors=list(self._connectors.keys()))

        for name, connector in self._connectors.items():
            self._stats[name] = {"received": 0, "published": 0, "rejected": 0}
            try:
                await connector.connect()
                symbols = self._get_symbols_for_connector(name)
                data_types = self._get_data_types_for_connector(name)
                await connector.subscribe(symbols, data_types)
                task = asyncio.create_task(
                    self._ingest_loop(connector),
                    name=f"ingest_{name}",
                )
                self._tasks.append(task)
                logger.info("connector_started", connector=name, symbols=symbols)
            except Exception as e:
                logger.error("connector_start_failed", connector=name, error=str(e))

    async def _ingest_loop(self, connector: DataConnector) -> None:
        """Consume data from a single connector and publish to Kafka."""
        name = connector.name
        retry_delay = 1.0
        max_retry_delay = 60.0

        while self._running:
            try:
                async for raw_event in connector.stream():
                    if not self._running:
                        break

                    self._stats[name]["received"] += 1
                    retry_delay = 1.0  # Reset on success

                    # 1. Validate
                    if not self._validate_event(raw_event):
                        self._stats[name]["rejected"] += 1
                        continue

                    # 2. Determine Kafka topic
                    asset_class = raw_event.get("asset_class", connector.asset_class.value)
                    symbol = raw_event.get("symbol", "unknown")
                    data_type = raw_event.get("data_type", "unknown")
                    topic = f"{asset_class}.{symbol.replace('/', '')}.{data_type}"

                    # 3. Publish to Kafka
                    await self._producer.send(topic, raw_event)
                    self._stats[name]["published"] += 1

                    # 4. Update Redis hot cache
                    await self._update_cache(raw_event)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    "ingest_loop_error",
                    connector=name,
                    error=str(e),
                    retry_in=retry_delay,
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, max_retry_delay)

                # Attempt reconnection
                try:
                    await connector.disconnect()
                    await connector.connect()
                    symbols = self._get_symbols_for_connector(name)
                    data_types = self._get_data_types_for_connector(name)
                    await connector.subscribe(symbols, data_types)
                    logger.info("connector_reconnected", connector=name)
                except Exception as re:
                    logger.error("reconnection_failed", connector=name, error=str(re))

    def _validate_event(self, event: Dict[str, Any]) -> bool:
        """Validate a raw event based on its data type."""
        data_type = event.get("data_type", "")
        symbol = event.get("symbol", "")

        if data_type in ("tick", "quote"):
            result = self._validator.validate_tick(
                symbol=symbol,
                bid=event.get("bid", 0),
                ask=event.get("ask", 0),
                last=event.get("last", event.get("price", 0)),
                timestamp=event.get("timestamp"),
            )
            return result.is_valid

        elif data_type in ("bar", "ohlcv"):
            result = self._validator.validate_bar(
                symbol=symbol,
                open_=event.get("open", 0),
                high=event.get("high", 0),
                low=event.get("low", 0),
                close=event.get("close", 0),
                volume=event.get("volume", 0),
            )
            return result.is_valid

        elif data_type == "orderbook":
            result = self._validator.validate_orderbook(
                symbol=symbol,
                bids=event.get("bids", []),
                asks=event.get("asks", []),
            )
            return result.is_valid

        # Trade and other types pass through
        return True

    async def _update_cache(self, event: Dict[str, Any]) -> None:
        """Update Redis hot cache based on event type."""
        try:
            data_type = event.get("data_type", "")
            symbol = event.get("symbol", "")

            if data_type == "orderbook":
                await self._cache.set_orderbook(symbol, event)
            elif data_type in ("tick", "quote", "trade"):
                await self._cache.push_tick(symbol, event)
        except Exception as e:
            logger.debug("cache_update_failed", error=str(e))

    def _get_symbols_for_connector(self, connector_name: str) -> List[str]:
        """Get configured symbols for a connector."""
        connector = self._connectors[connector_name]
        ac = connector.asset_class.value
        sym_config = getattr(settings, "symbols", {})
        if isinstance(sym_config, dict):
            return sym_config.get(ac, [])
        return []

    def _get_data_types_for_connector(self, connector_name: str) -> List[str]:
        """Get configured data types for a connector."""
        # Default data types per connector
        defaults = {
            "ccxt": ["trade", "orderbook", "bar"],
            "polygon": ["trade", "quote", "bar"],
            "interactive_brokers": ["tick", "bar"],
        }
        for key, value in defaults.items():
            if key in connector_name.lower():
                return value
        return ["trade", "bar"]

    async def stop(self) -> None:
        """Stop all ingestion loops and disconnect connectors."""
        self._running = False
        for task in self._tasks:
            task.cancel()

        # Wait for tasks to finish
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        for name, connector in self._connectors.items():
            try:
                await connector.disconnect()
            except Exception as e:
                logger.warning("disconnect_error", connector=name, error=str(e))

        logger.info("ingest_manager_stopped", stats=self._stats)

    def get_stats(self) -> Dict[str, Dict[str, int]]:
        """Return ingestion statistics per connector."""
        return self._stats.copy()

    async def health_check(self) -> Dict[str, bool]:
        """Check health of all connectors."""
        results = {}
        for name, connector in self._connectors.items():
            try:
                results[name] = await connector.health_check()
            except Exception:
                results[name] = False
        return results
