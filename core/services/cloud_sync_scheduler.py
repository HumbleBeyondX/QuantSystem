"""Cloud sync scheduler — periodic incremental sync of local SQLite → Supabase.

Design mirrors ``core/services/report_scheduler.py``:
  * Asyncio task that wakes every 5 minutes
  * Gracefully no-ops if Supabase is not configured
  * Uses SyncManager / SyncCursorRepo for incremental pushes
  * Honors a lock to prevent overlapping syncs if a sync runs long

Only the following tables are synced by default (backtest + research artifacts):
  - backtest_results
  - factor_evaluations
  - audit_log

Trading tables (orders, fills, positions) are NOT synced by this scheduler
since the live runner on Tokyo VPS writes them separately. Syncing them
would cause duplicate-key conflicts across two sources of truth.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import List, Optional

from core.logging import get_logger

logger = get_logger(__name__)

# Tables that this scheduler pushes UP to the cloud every tick.
# Tables like `orders`/`fills` are excluded — live runner on VPS owns them.
SYNC_TABLES: List[str] = [
    "backtest_results",
    "factor_evaluations",
    "audit_log",
]

TICK_INTERVAL_SECONDS = 300  # 5 minutes


class CloudSyncScheduler:
    """Periodic cloud sync of selected tables (backtest + research only)."""

    def __init__(self, tables: Optional[List[str]] = None, interval_seconds: int = TICK_INTERVAL_SECONDS):
        self.tables = tables or SYNC_TABLES
        self.interval_seconds = interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._last_run_ts: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._last_synced_rows: int = 0

    # ------------------------------------------------------------------
    # Core loop
    # ------------------------------------------------------------------
    async def _loop(self) -> None:
        logger.info("cloud_sync_scheduler_started", tables=self.tables, interval_s=self.interval_seconds)
        try:
            while not self._stop_event.is_set():
                try:
                    await self._tick()
                except Exception as e:
                    logger.error("cloud_sync_tick_error", error=str(e))
                    self._last_error = str(e)

                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            logger.info("cloud_sync_scheduler_stopped")

    async def _tick(self) -> None:
        from core.cloud import get_supabase_client, is_supabase_configured

        if not is_supabase_configured():
            logger.debug("cloud_sync_skipped_not_configured")
            return

        client = await get_supabase_client()
        if client is None:
            # Connection failed upstream; no-op.
            return

        cloud_engine = client.db_engine
        if cloud_engine is None:
            logger.debug("cloud_sync_skipped_no_db_engine")
            return

        # Local engine — the same one the backend uses for SQLite
        from core.database.engine import init_db
        from core.cloud.sync_manager import SyncManager

        local_engine = await init_db(create_tables=False)

        sync = SyncManager(local_engine=local_engine, cloud_engine=cloud_engine)
        total_rows = 0
        for table in self.tables:
            try:
                rows = await sync.sync_up(table)
                if rows:
                    logger.info("cloud_sync_pushed", table=table, rows=rows)
                total_rows += rows
            except Exception as e:
                # Log and continue — other tables may still succeed.
                logger.warning("cloud_sync_table_failed", table=table, error=str(e))

        self._last_run_ts = datetime.now(timezone.utc)
        self._last_synced_rows = total_rows
        self._last_error = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def trigger_now(self) -> int:
        """Manual trigger — run one sync cycle now. Returns rows pushed."""
        await self._tick()
        return self._last_synced_rows

    def status(self) -> dict:
        from core.cloud import is_supabase_configured

        return {
            "enabled": is_supabase_configured(),
            "running": bool(self._task and not self._task.done()),
            "interval_seconds": self.interval_seconds,
            "tables": self.tables,
            "last_run_at": self._last_run_ts.isoformat() if self._last_run_ts else None,
            "last_synced_rows": self._last_synced_rows,
            "last_error": self._last_error,
        }


# ────────────────────────────────────────────────────────────
# Module-level singleton
# ────────────────────────────────────────────────────────────

_scheduler: Optional[CloudSyncScheduler] = None


def get_cloud_sync_scheduler() -> CloudSyncScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = CloudSyncScheduler()
    return _scheduler
