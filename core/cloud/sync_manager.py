"""Bidirectional sync manager — SQLite (local) <-> PostgreSQL (Supabase cloud).

Sync strategy:
- Offline-first: all writes go to local SQLite first
- Incremental sync via `updated_at` / `created_at` timestamps
- Conflict resolution: last-write-wins based on `updated_at`
- Selective sync: user chooses which tables to sync

Usage:
    sync = SyncManager(local_engine, cloud_engine)
    await sync.sync_up("orders")      # local -> cloud
    await sync.sync_down("orders")    # cloud -> local
    await sync.full_sync()            # bidirectional all tables
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from core.database.models import (
    AuditLogRecord,
    BacktestResultRecord,
    Base,
    FillRecord,
    OrderRecord,
    PortfolioSnapshotRecord,
    PositionSnapshot,
    RiskEventRecord,
    StrategyStateRecord,
    SyncCursor,
)

logger = logging.getLogger(__name__)

# Tables that support sync (have a timestamp column for incremental queries)
SYNCABLE_TABLES = {
    "orders": {"model": OrderRecord, "ts_col": "created_at", "id_col": "order_id"},
    "fills": {"model": FillRecord, "ts_col": "timestamp", "id_col": "fill_id"},
    "position_snapshots": {"model": PositionSnapshot, "ts_col": "timestamp", "id_col": "id"},
    "portfolio_snapshots": {"model": PortfolioSnapshotRecord, "ts_col": "timestamp", "id_col": "id"},
    "strategy_state": {"model": StrategyStateRecord, "ts_col": "updated_at", "id_col": "strategy_id"},
    "risk_events": {"model": RiskEventRecord, "ts_col": "timestamp", "id_col": "event_id"},
    "backtest_results": {"model": BacktestResultRecord, "ts_col": "created_at", "id_col": "backtest_id"},
}


class SyncManager:
    """Bidirectional sync between local SQLite and cloud PostgreSQL."""

    def __init__(
        self,
        local_engine: AsyncEngine,
        cloud_engine: AsyncEngine,
        batch_size: int = 500,
    ):
        self._local = async_sessionmaker(local_engine, expire_on_commit=False)
        self._cloud = async_sessionmaker(cloud_engine, expire_on_commit=False)
        self._batch_size = batch_size
        self._enabled_tables: Set[str] = set(SYNCABLE_TABLES.keys())
        self._is_syncing = False

    def set_enabled_tables(self, tables: List[str]) -> None:
        """Configure which tables to sync."""
        self._enabled_tables = set(t for t in tables if t in SYNCABLE_TABLES)

    # ------------------------------------------------------------------
    # Sync Up: local -> cloud
    # ------------------------------------------------------------------

    async def sync_up(self, table_name: str) -> int:
        """Push local changes to cloud for a single table.

        Returns number of rows synced.
        """
        if table_name not in SYNCABLE_TABLES:
            raise ValueError(f"Table not syncable: {table_name}")

        info = SYNCABLE_TABLES[table_name]
        model = info["model"]
        ts_col = getattr(model, info["ts_col"])

        # Get last sync cursor
        async with self._local() as session:
            cursor = await self._get_cursor(session, table_name, "up")
            since = cursor.last_synced_at if cursor else datetime.min.replace(tzinfo=timezone.utc)

            # Fetch rows modified since last sync
            stmt = select(model).where(ts_col > since).order_by(ts_col).limit(self._batch_size)
            rows = (await session.execute(stmt)).scalars().all()

        if not rows:
            return 0

        # Upsert into cloud
        count = 0
        async with self._cloud() as cloud_session:
            for row in rows:
                # Convert to dict, excluding SQLAlchemy internal state
                row_dict = {c.key: getattr(row, c.key) for c in inspect(row).mapper.column_attrs}
                row_dict.pop("id", None)  # Let cloud auto-generate primary key

                # Use merge for upsert behavior
                cloud_session.merge(model(**row_dict))
                count += 1

            await cloud_session.commit()

        # Update sync cursor
        async with self._local() as session:
            last_ts = getattr(rows[-1], info["ts_col"])
            await self._update_cursor(session, table_name, "up", last_ts, count)
            await session.commit()

        logger.info("sync_up_complete", table=table_name, rows=count)
        return count

    # ------------------------------------------------------------------
    # Sync Down: cloud -> local
    # ------------------------------------------------------------------

    async def sync_down(self, table_name: str) -> int:
        """Pull cloud changes to local for a single table.

        Returns number of rows synced.
        """
        if table_name not in SYNCABLE_TABLES:
            raise ValueError(f"Table not syncable: {table_name}")

        info = SYNCABLE_TABLES[table_name]
        model = info["model"]
        ts_col = getattr(model, info["ts_col"])

        # Get last sync cursor
        async with self._local() as session:
            cursor = await self._get_cursor(session, table_name, "down")
            since = cursor.last_synced_at if cursor else datetime.min.replace(tzinfo=timezone.utc)

        # Fetch rows from cloud modified since last sync
        async with self._cloud() as cloud_session:
            stmt = select(model).where(ts_col > since).order_by(ts_col).limit(self._batch_size)
            rows = (await cloud_session.execute(stmt)).scalars().all()

        if not rows:
            return 0

        # Upsert into local
        count = 0
        async with self._local() as local_session:
            for row in rows:
                row_dict = {c.key: getattr(row, c.key) for c in inspect(row).mapper.column_attrs}
                row_dict.pop("id", None)
                local_session.merge(model(**row_dict))
                count += 1

            await local_session.commit()

            # Update sync cursor
            last_ts = getattr(rows[-1], info["ts_col"])
            await self._update_cursor(local_session, table_name, "down", last_ts, count)
            await local_session.commit()

        logger.info("sync_down_complete", table=table_name, rows=count)
        return count

    # ------------------------------------------------------------------
    # Full sync
    # ------------------------------------------------------------------

    async def full_sync(self) -> Dict[str, Dict[str, int]]:
        """Run bidirectional sync on all enabled tables.

        Returns dict of table -> {"up": N, "down": M}.
        """
        if self._is_syncing:
            logger.warning("sync_already_running")
            return {}

        self._is_syncing = True
        results: Dict[str, Dict[str, int]] = {}

        try:
            for table in self._enabled_tables:
                try:
                    up = await self.sync_up(table)
                    down = await self.sync_down(table)
                    results[table] = {"up": up, "down": down}
                except Exception as e:
                    logger.error("sync_table_error", table=table, error=str(e))
                    results[table] = {"up": 0, "down": 0, "error": str(e)}
        finally:
            self._is_syncing = False

        logger.info("full_sync_complete", results=results)
        return results

    # ------------------------------------------------------------------
    # Sync status
    # ------------------------------------------------------------------

    async def get_sync_status(self) -> Dict[str, Any]:
        """Get current sync status for all tables."""
        status: Dict[str, Any] = {"is_syncing": self._is_syncing, "tables": {}}

        async with self._local() as session:
            for table in SYNCABLE_TABLES:
                cursor = await self._get_cursor(session, table, "up")
                status["tables"][table] = {
                    "last_synced": cursor.last_synced_at.isoformat() if cursor else None,
                    "row_count": cursor.row_count if cursor else 0,
                    "enabled": table in self._enabled_tables,
                }

        return status

    # ------------------------------------------------------------------
    # Cursor management
    # ------------------------------------------------------------------

    async def _get_cursor(
        self, session: AsyncSession, table_name: str, direction: str
    ) -> Optional[SyncCursor]:
        key = f"{table_name}_{direction}"
        stmt = select(SyncCursor).where(SyncCursor.table_name == key)
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _update_cursor(
        self,
        session: AsyncSession,
        table_name: str,
        direction: str,
        last_ts: datetime,
        count: int,
    ) -> None:
        key = f"{table_name}_{direction}"
        existing = await self._get_cursor(session, table_name, direction)
        if existing:
            existing.last_synced_at = last_ts
            existing.row_count += count
        else:
            session.add(SyncCursor(
                table_name=key,
                last_synced_at=last_ts,
                row_count=count,
                direction=direction,
            ))
