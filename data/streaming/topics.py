"""Kafka topic registry and management.

Topic naming convention: {asset_class}.{symbol}.{data_type}
Examples:
    crypto.BTCUSDT.orderbook
    crypto.ETHUSDT.trade
    equity.AAPL.bar
    options.SPY.chain
    system.health
    signals.crypto_scalping_v1
"""

from __future__ import annotations

from typing import Dict, List

from aiokafka.admin import AIOKafkaAdminClient, NewTopic

from core.config import settings
from core.logging import get_logger

logger = get_logger(__name__)


def make_topic_name(asset_class: str, symbol: str, data_type: str) -> str:
    """Generate a standardized topic name.

    Args:
        asset_class: crypto | equity | options
        symbol: Trading symbol (slashes removed)
        data_type: tick | trade | bar | orderbook | ohlcv | quote
    """
    safe_symbol = symbol.replace("/", "").replace(" ", "")
    return f"{asset_class}.{safe_symbol}.{data_type}"


# Pre-defined system topics
SYSTEM_TOPICS = [
    "system.health",
    "system.alerts",
    "signals.all",
    "orders.all",
    "fills.all",
    "risk.alerts",
]


def get_all_data_topics() -> List[str]:
    """Generate all data topics based on configured symbols and data types."""
    topics: List[str] = []

    connector_config = settings.features  # Use connector config from settings
    symbols = settings.symbols

    # Crypto topics
    for symbol in symbols.get("crypto", []):
        for dtype in ["trade", "orderbook", "ohlcv"]:
            topics.append(make_topic_name("crypto", symbol, dtype))

    # Equity topics
    for symbol in symbols.get("equity", []):
        for dtype in ["tick", "bar", "quote"]:
            topics.append(make_topic_name("equity", symbol, dtype))

    # Options topics
    for symbol in symbols.get("options", []):
        for dtype in ["chain", "greeks", "unusual_activity"]:
            topics.append(make_topic_name("options", symbol, dtype))

    return topics


async def ensure_topics(
    num_partitions: int = 3,
    replication_factor: int = 1,
) -> None:
    """Create all required Kafka topics if they don't exist."""
    admin = AIOKafkaAdminClient(
        bootstrap_servers=settings.kafka.bootstrap_servers,
    )
    await admin.start()

    try:
        existing = await admin.list_topics()
        all_topics = SYSTEM_TOPICS + get_all_data_topics()

        new_topics = [
            NewTopic(
                name=topic,
                num_partitions=num_partitions,
                replication_factor=replication_factor,
            )
            for topic in all_topics
            if topic not in existing
        ]

        if new_topics:
            await admin.create_topics(new_topics)
            logger.info("topics_created", count=len(new_topics))
        else:
            logger.info("topics_already_exist", count=len(all_topics))
    finally:
        await admin.close()
