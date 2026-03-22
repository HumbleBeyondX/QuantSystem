"""Async Kafka consumer for subscribing to market data and events."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine, Dict, List, Optional

from aiokafka import AIOKafkaConsumer

from core.config import settings
from core.logging import get_logger
from core.utils.serialization import from_json

logger = get_logger(__name__)

# Handler type
MessageHandler = Callable[[str, Dict[str, Any]], Coroutine[Any, Any, None]]


class KafkaConsumerWrapper:
    """Async Kafka consumer with message routing.

    Usage:
        consumer = KafkaConsumerWrapper(topics=["crypto.BTCUSDT.trade"])
        consumer.on_message(handler_func)
        await consumer.start()
    """

    def __init__(
        self,
        topics: List[str],
        group_id: Optional[str] = None,
    ) -> None:
        self._topics = topics
        self._group_id = group_id or settings.kafka.consumer_group
        self._consumer: Optional[AIOKafkaConsumer] = None
        self._handlers: List[MessageHandler] = []
        self._running = False

    def on_message(self, handler: MessageHandler) -> None:
        """Register a message handler."""
        self._handlers.append(handler)

    async def start(self) -> None:
        """Start consuming messages."""
        self._consumer = AIOKafkaConsumer(
            *self._topics,
            bootstrap_servers=settings.kafka.bootstrap_servers,
            group_id=self._group_id,
            auto_offset_reset=settings.kafka.auto_offset_reset,
            value_deserializer=lambda m: from_json(m),
            enable_auto_commit=True,
            auto_commit_interval_ms=1000,
        )
        await self._consumer.start()
        self._running = True
        logger.info("kafka_consumer_started", topics=self._topics, group=self._group_id)

        # Start consuming loop
        asyncio.create_task(self._consume_loop())

    async def _consume_loop(self) -> None:
        """Main consumption loop."""
        try:
            async for msg in self._consumer:
                topic = msg.topic
                value = msg.value

                for handler in self._handlers:
                    try:
                        await handler(topic, value)
                    except Exception as e:
                        logger.error(
                            "consumer_handler_error",
                            topic=topic,
                            error=str(e),
                        )
        except Exception as e:
            if self._running:
                logger.error("consumer_loop_error", error=str(e))

    async def stop(self) -> None:
        """Stop the consumer."""
        self._running = False
        if self._consumer:
            await self._consumer.stop()
            logger.info("kafka_consumer_stopped")
