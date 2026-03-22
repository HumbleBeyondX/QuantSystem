"""System health and infrastructure endpoints."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def system_health():
    """Check health of all system components."""
    return {
        "kafka": "unknown",
        "timescaledb": "unknown",
        "redis": "unknown",
        "minio": "unknown",
        "connectors": {},
    }


@router.get("/stats")
async def system_stats():
    """Get system statistics."""
    return {
        "uptime_seconds": 0,
        "messages_processed": 0,
        "active_strategies": 0,
        "open_positions": 0,
    }
