"""TimescaleDB client for warm storage of time-series data.

Uses asyncpg for async PostgreSQL access. TimescaleDB hypertables
provide automatic time-based partitioning and compression.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncpg
import pandas as pd

from core.config import settings
from core.logging import get_logger
from core.models import Bar, Fill, Order, Position

logger = get_logger(__name__)


class TimescaleClient:
    """Async TimescaleDB client for reading/writing time-series data."""

    def __init__(self) -> None:
        self._pool: Optional[asyncpg.Pool] = None

    async def connect(self) -> None:
        """Create connection pool."""
        self._pool = await asyncpg.create_pool(
            dsn=settings.timescaledb.dsn,
            min_size=settings.timescaledb.pool_min,
            max_size=settings.timescaledb.pool_max,
        )
        logger.info("timescaledb_connected", dsn=settings.timescaledb.dsn)

    async def disconnect(self) -> None:
        """Close connection pool."""
        if self._pool:
            await self._pool.close()
            logger.info("timescaledb_disconnected")

    async def run_migrations(self) -> None:
        """Execute all SQL migration files in order."""
        migrations_dir = Path(__file__).parent / "migrations"
        if not migrations_dir.exists():
            logger.warning("no_migrations_dir")
            return

        migration_files = sorted(migrations_dir.glob("*.sql"))
        async with self._pool.acquire() as conn:
            for migration_file in migration_files:
                sql = migration_file.read_text()
                try:
                    await conn.execute(sql)
                    logger.info("migration_applied", file=migration_file.name)
                except Exception as e:
                    logger.error("migration_failed", file=migration_file.name, error=str(e))
                    raise

    # ============================================================
    # OHLCV Operations
    # ============================================================

    async def insert_bars(self, bars: List[Bar]) -> int:
        """Bulk insert OHLCV bars."""
        if not bars:
            return 0
        async with self._pool.acquire() as conn:
            rows = [
                (
                    b.timestamp, b.symbol, b.asset_class.value, b.timeframe.value,
                    b.open, b.high, b.low, b.close, b.volume, b.vwap,
                )
                for b in bars
            ]
            await conn.executemany(
                """
                INSERT INTO ohlcv (timestamp, symbol, asset_class, timeframe,
                                   open, high, low, close, volume, vwap)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (timestamp, symbol, timeframe) DO NOTHING
                """,
                rows,
            )
            logger.debug("bars_inserted", count=len(rows), symbol=bars[0].symbol)
            return len(rows)

    async def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        limit: int = 10000,
    ) -> pd.DataFrame:
        """Retrieve OHLCV bars as DataFrame."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT timestamp, symbol, open, high, low, close, volume, vwap
                FROM ohlcv
                WHERE symbol = $1 AND timeframe = $2
                  AND timestamp >= $3 AND timestamp < $4
                ORDER BY timestamp ASC
                LIMIT $5
                """,
                symbol, timeframe, start, end, limit,
            )
            if not rows:
                return pd.DataFrame()
            return pd.DataFrame([dict(r) for r in rows]).set_index("timestamp")

    # ============================================================
    # Trades Operations
    # ============================================================

    async def insert_trades(self, trades: List[Dict[str, Any]]) -> int:
        """Bulk insert raw trade data."""
        if not trades:
            return 0
        async with self._pool.acquire() as conn:
            rows = [
                (t["timestamp"], t["symbol"], t["price"], t["amount"], t["side"], t.get("source", ""))
                for t in trades
            ]
            await conn.executemany(
                """
                INSERT INTO trades (timestamp, symbol, price, amount, side, source)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                rows,
            )
            return len(rows)

    # ============================================================
    # Features Operations
    # ============================================================

    async def insert_features(
        self, symbol: str, timestamp: datetime, features: Dict[str, float]
    ) -> None:
        """Insert computed feature values."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO features (timestamp, symbol, feature_name, feature_value)
                SELECT $1, $2, key, value::float
                FROM jsonb_each_text($3::jsonb)
                ON CONFLICT (timestamp, symbol, feature_name) DO UPDATE
                SET feature_value = EXCLUDED.feature_value
                """,
                timestamp,
                symbol,
                str(features).replace("'", '"'),
            )

    async def get_features(
        self,
        symbol: str,
        feature_names: List[str],
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Retrieve feature values as wide-format DataFrame."""
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT timestamp, feature_name, feature_value
                FROM features
                WHERE symbol = $1
                  AND feature_name = ANY($2)
                  AND timestamp >= $3 AND timestamp < $4
                ORDER BY timestamp ASC
                """,
                symbol, feature_names, start, end,
            )
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame([dict(r) for r in rows])
            return df.pivot(index="timestamp", columns="feature_name", values="feature_value")

    # ============================================================
    # Orders & Positions (Audit)
    # ============================================================

    async def log_order(self, order: Order) -> None:
        """Log an order event to the audit table."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO orders_log (order_id, timestamp, strategy_id, symbol,
                                        side, order_type, quantity, status,
                                        limit_price, filled_quantity, average_fill_price, commission)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                """,
                str(order.order_id), order.updated_at, order.strategy_id, order.symbol,
                order.side.value, order.order_type.value, order.quantity, order.status.value,
                order.limit_price, order.filled_quantity, order.average_fill_price, order.commission,
            )

    async def execute(self, query: str, *args: Any) -> Any:
        """Execute a raw SQL query."""
        async with self._pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def health_check(self) -> bool:
        """Check database connectivity."""
        try:
            async with self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False
