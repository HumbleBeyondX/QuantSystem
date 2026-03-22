"""Async Kafka producer for publishing market data and events."""

from __future__ import annotations

from typing import Any, Dict, Optional

from aiokafka import AIOKafkaProducer

from core.config import settings
from core.logging import get_logger
from core.utils.serialization import to_json_bytes

logger = get_logger(__name__)


class KafkaProducerWrapper:
    """Async Kafka producer with batching and serialization."""

    def __init__(self) -> None:
        self._producer: Optional[AIOKafkaProducer] = None

    async def start(self) -> None:
        """Start the Kafka producer."""
        self._producer = AIOKafkaProducer(
            bootstrap_servers=settings.kafka.bootstrap_servers,
            value_serializer=to_json_bytes,
            key_serializer=lambda k: k.encode("utf-8") if k else None,
            linger_ms=settings.kafka.linger_ms,
            max_batch_size=16384 * 4,  # 64KB batches
            acks="all",
            compression_type="snappy",
        )
        await self._producer.start()
        logger.info("kafka_producer_started")

    async def stop(self) -> None:
        """Flush and stop the producer."""
        if self._producer:
            await self._producer.stop()
            logger.info("kafka_producer_stopped")

    async def send(
        self,
        topic: str,
        value: Dict[str, Any],
        key: Optional[str] = None,
    ) -> None:
        """Send a message to a Kafka topic.

        Args:
            topic: Target topic name
            value: Message payload (will be JSON-serialized)
            key: Optional message key for partitioning (e.g., symbol)
        """
        if not self._producer:
            raise RuntimeError("Producer not started")

        await self._producer.send(topic, value=value, key=key)

    async def send_and_wait(
        self,
        topic: str,
        value: Dict[str, Any],
        key: Optional[str] = None,
    ) -> None:
        """Send a message and wait for broker acknowledgment."""
        if not self._producer:
            raise RuntimeError("Producer not started")

        await self._producer.send_and_wait(topic, value=value, key=key)

    async def flush(self) -> None:
        """Flush all buffered messages."""
        if self._producer:
            await self._producer.flush()
