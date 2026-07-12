"""Cloud sync layer — Supabase integration for bidirectional data sync.

Gracefully degrading: if SUPABASE_URL is not set, all accessors return None
and callers should treat cloud sync as disabled.
"""

from __future__ import annotations

import os
from typing import Optional

from core.logging import get_logger

logger = get_logger(__name__)

_client_instance: Optional["SupabaseClient"] = None  # type: ignore[name-defined]
_connection_attempted: bool = False


def is_supabase_configured() -> bool:
    """True if the minimum Supabase env vars are present."""
    return bool(
        os.environ.get("SUPABASE_URL")
        and os.environ.get("SUPABASE_ANON_KEY")
    )


async def get_supabase_client() -> Optional["SupabaseClient"]:  # type: ignore[name-defined]
    """Get or create the shared SupabaseClient. Returns None if not configured.

    Call sites must check the return value:
        client = await get_supabase_client()
        if client is None:
            return  # cloud sync disabled
    """
    global _client_instance, _connection_attempted

    if _client_instance is not None:
        return _client_instance

    if _connection_attempted:
        # Already tried and failed — don't retry every call.
        return None

    if not is_supabase_configured():
        logger.debug("supabase_not_configured")
        _connection_attempted = True
        return None

    try:
        from core.cloud.supabase_client import SupabaseClient

        client = SupabaseClient()
        await client.connect()
        _client_instance = client
        logger.info("supabase_client_ready")
        return client
    except Exception as e:
        logger.warning("supabase_connect_failed", error=str(e))
        _connection_attempted = True
        return None


async def close_supabase_client() -> None:
    """Dispose of the shared client. Call on shutdown."""
    global _client_instance, _connection_attempted
    if _client_instance is not None:
        try:
            await _client_instance.disconnect()
        except Exception as e:
            logger.warning("supabase_disconnect_failed", error=str(e))
        _client_instance = None
    _connection_attempted = False


__all__ = [
    "is_supabase_configured",
    "get_supabase_client",
    "close_supabase_client",
]
