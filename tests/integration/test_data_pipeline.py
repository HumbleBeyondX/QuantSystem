"""Integration test stubs for the data pipeline.

These tests require running infrastructure (Kafka, Redis, data connectors)
and are marked with ``pytest.mark.integration`` so they can be excluded
from fast unit-test runs.

Run with::

    pytest -m integration tests/integration/
"""

from __future__ import annotations

import pytest


@pytest.mark.integration
class TestKafkaPipeline:
    async def test_kafka_produce_consume(self):
        """Verify that a message produced to a Kafka topic can be consumed."""
        # Arrange
        # topic = "test.market_data"
        # producer = KafkaProducer(bootstrap_servers=...)
        # consumer = KafkaConsumer(topic, bootstrap_servers=...)

        # Act
        # await producer.send(topic, value={"symbol": "AAPL", "price": 192.0})
        # message = await consumer.receive(timeout=5.0)

        # Assert
        # assert message is not None
        # assert message["symbol"] == "AAPL"
        pytest.skip("Requires running Kafka instance")


@pytest.mark.integration
class TestRedisCache:
    async def test_redis_cache_orderbook(self):
        """Verify that an order book snapshot can be cached and retrieved from Redis."""
        # Arrange
        # cache = RedisCache(host="localhost", port=6379)

        # Act
        # await cache.set_orderbook("BTC/USDT", snapshot)
        # result = await cache.get_orderbook("BTC/USDT")

        # Assert
        # assert result is not None
        # assert result.best_bid == snapshot.best_bid
        pytest.skip("Requires running Redis instance")


@pytest.mark.integration
class TestConnectorToKafka:
    async def test_connector_to_kafka(self):
        """Verify end-to-end flow: connector -> normaliser -> Kafka."""
        # Arrange
        # connector = PolygonConnector(api_key=os.getenv("POLYGON_API_KEY"))
        # producer = KafkaProducer(...)

        # Act
        # await connector.connect()
        # async for event in connector.stream():
        #     await producer.send("market_data", event)
        #     break  # just verify one event flows through

        # Assert
        # assert event is not None
        pytest.skip("Requires running Kafka and valid API key")
