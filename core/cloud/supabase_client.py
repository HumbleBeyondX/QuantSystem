"""Supabase client wrapper — manages connection to Supabase PostgreSQL + Storage.

Provides:
- Async SQLAlchemy engine pointing at Supabase PostgreSQL
- Storage client for large files (backtest results, logs)
- Connection health check

Requirements:
    pip install supabase asyncpg
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class SupabaseClient:
    """Wrapper around Supabase for cloud operations.

    Uses:
    - SQLAlchemy asyncpg engine for database operations (same ORM as local)
    - Supabase Python SDK for storage (S3-compatible bucket)
    """

    def __init__(
        self,
        url: Optional[str] = None,
        anon_key: Optional[str] = None,
        service_key: Optional[str] = None,
        db_url: Optional[str] = None,
    ):
        self._url = url or os.environ.get("SUPABASE_URL", "")
        self._anon_key = anon_key or os.environ.get("SUPABASE_ANON_KEY", "")
        self._service_key = service_key or os.environ.get("SUPABASE_SERVICE_KEY", "")
        self._db_url = db_url or os.environ.get("SUPABASE_DB_URL", "")
        self._client: Any = None
        self._db_engine: Any = None

    async def connect(self) -> None:
        """Initialize Supabase client and database engine."""
        if not self._url or not self._anon_key:
            raise ValueError(
                "Supabase URL and anon key required. "
                "Set SUPABASE_URL and SUPABASE_ANON_KEY environment variables."
            )

        try:
            from supabase import create_client
            self._client = create_client(self._url, self._anon_key)
            logger.info("supabase_client_connected", url=self._url)
        except ImportError:
            raise ImportError("supabase not installed. Run: pip install supabase")

        # Set up async database engine if DB URL provided
        if self._db_url:
            from core.database.engine import build_engine
            self._db_engine = build_engine(self._db_url)
            logger.info("supabase_db_engine_connected")

    async def disconnect(self) -> None:
        """Clean up connections."""
        if self._db_engine:
            await self._db_engine.dispose()
        self._client = None
        self._db_engine = None

    @property
    def client(self):
        """Raw Supabase client for direct operations."""
        if not self._client:
            raise RuntimeError("Supabase client not connected")
        return self._client

    @property
    def db_engine(self):
        """Async SQLAlchemy engine pointing at Supabase PostgreSQL."""
        return self._db_engine

    # ------------------------------------------------------------------
    # Storage operations
    # ------------------------------------------------------------------

    async def upload_file(
        self,
        bucket: str,
        path: str,
        data: bytes,
        content_type: str = "application/json",
    ) -> str:
        """Upload a file to Supabase Storage.

        Returns the public URL of the uploaded file.
        """
        if not self._client:
            raise RuntimeError("Supabase client not connected")

        result = self._client.storage.from_(bucket).upload(
            path, data, {"content-type": content_type}
        )
        return f"{self._url}/storage/v1/object/public/{bucket}/{path}"

    async def download_file(self, bucket: str, path: str) -> bytes:
        """Download a file from Supabase Storage."""
        if not self._client:
            raise RuntimeError("Supabase client not connected")

        return self._client.storage.from_(bucket).download(path)

    async def list_files(self, bucket: str, prefix: str = "") -> list:
        """List files in a storage bucket."""
        if not self._client:
            raise RuntimeError("Supabase client not connected")

        return self._client.storage.from_(bucket).list(prefix)

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Check Supabase connectivity."""
        if not self._client:
            return False
        try:
            # Simple query to verify connection
            self._client.table("sync_cursors").select("id").limit(1).execute()
            return True
        except Exception:
            return False
