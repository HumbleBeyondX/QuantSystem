"""Redis hot cache for orderbook snapshots, tick buffers, and strategy state.

Provides sub-millisecond reads for latency-sensitive operations like
scalping strategies that need real-time orderbook data.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Dict, List, Optional

import redis.asyncio as aioredis

from core.config import settings
from core.logging import get_logger
from core.utils.serialization import QuantJSONEncoder

logger = get_logger(__name__)


class RedisCache:
    """Async Redis client for hot data caching."""

    # Key prefixes
    ORDERBOOK_PREFIX = "ob:"       # ob:{symbol} -> JSON orderbook snapshot
    TICK_PREFIX = "tick:"           # tick:{symbol} -> list of recent ticks
    STATE_PREFIX = "state:"        # state:{strategy_id} -> strategy state dict
    FEATURE_PREFIX = "feat:"       # feat:{symbol} -> latest feature values
    LOCK_PREFIX = "lock:"          # lock:{resource} -> distributed lock

    def __init__(self) -> None:
        self._client: Optional[aioredis.Redis] = None

    async def connect(self) -> None:
        """Create Redis connection pool."""
        self._client = aioredis.from_url(
            settings.redis.url,
            max_connections=settings.redis.max_connections,
            decode_responses=True,
        )
        await self._client.ping()
        logger.info("redis_connected", url=settings.redis.url)

    async def disconnect(self) -> None:
        """Close Redis connection."""
        if self._client:
            await self._client.close()
            logger.info("redis_disconnected")

    # ============================================================
    # Orderbook Cache
    # ============================================================

    async def set_orderbook(self, symbol: str, orderbook: Dict[str, Any]) -> None:
        """Cache latest orderbook snapshot (auto-expires in 60s)."""
        key = f"{self.ORDERBOOK_PREFIX}{symbol}"
        await self._client.setex(
            key,
            timedelta(seconds=60),
            json.dumps(orderbook, cls=QuantJSONEncoder),
        )

    async def get_orderbook(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get cached orderbook snapshot."""
        key = f"{self.ORDERBOOK_PREFIX}{symbol}"
        data = await self._client.get(key)
        return json.loads(data) if data else None

    # ============================================================
    # Tick Buffer (Ring Buffer)
    # ============================================================

    async def push_tick(self, symbol: str, tick: Dict[str, Any], max_size: int = 1000) -> None:
        """Push a tick to the ring buffer, trimming old entries."""
        key = f"{self.TICK_PREFIX}{symbol}"
        await self._client.lpush(key, json.dumps(tick, cls=QuantJSONEncoder))
        await self._client.ltrim(key, 0, max_size - 1)
        await self._client.expire(key, 300)  # 5 min TTL

    async def get_recent_ticks(self, symbol: str, count: int = 100) -> List[Dict[str, Any]]:
        """Get recent ticks (newest first)."""
        key = f"{self.TICK_PREFIX}{symbol}"
        raw = await self._client.lrange(key, 0, count - 1)
        return [json.loads(item) for item in raw]

    # ============================================================
    # Strategy State
    # ============================================================

    async def set_strategy_state(self, strategy_id: str, state: Dict[str, Any]) -> None:
        """Store strategy runtime state."""
        key = f"{self.STATE_PREFIX}{strategy_id}"
        await self._client.set(key, json.dumps(state, cls=QuantJSONEncoder))

    async def get_strategy_state(self, strategy_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve strategy runtime state."""
        key = f"{self.STATE_PREFIX}{strategy_id}"
        data = await self._client.get(key)
        return json.loads(data) if data else None

    # ============================================================
    # Feature Cache
    # ============================================================

    async def set_features(self, symbol: str, features: Dict[str, float]) -> None:
        """Cache latest computed feature values."""
        key = f"{self.FEATURE_PREFIX}{symbol}"
        await self._client.setex(
            key, timedelta(seconds=120), json.dumps(features)
        )

    async def get_features(self, symbol: str) -> Optional[Dict[str, float]]:
        """Get cached feature values."""
        key = f"{self.FEATURE_PREFIX}{symbol}"
        data = await self._client.get(key)
        return json.loads(data) if data else None

    # ============================================================
    # Generic Operations
    # ============================================================

    async def set(self, key: str, value: Any, ttl_seconds: int = 300) -> None:
        """Generic set with TTL."""
        await self._client.setex(key, timedelta(seconds=ttl_seconds), json.dumps(value, cls=QuantJSONEncoder))

    async def get(self, key: str) -> Optional[Any]:
        """Generic get."""
        data = await self._client.get(key)
        return json.loads(data) if data else None

    async def delete(self, key: str) -> None:
        """Delete a key."""
        await self._client.delete(key)

    async def health_check(self) -> bool:
        """Check Redis connectivity."""
        try:
            return await self._client.ping()
        except Exception:
            return False
