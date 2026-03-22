"""Strategy API endpoints: list, status, signals, parameters."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def list_strategies():
    """List all registered strategies with their status."""
    return []


@router.get("/{strategy_id}")
async def get_strategy_detail(strategy_id: str):
    """Get detailed info about a specific strategy."""
    return {"strategy_id": strategy_id, "status": "not_found"}


@router.get("/{strategy_id}/signals")
async def get_strategy_signals(strategy_id: str, limit: int = 50):
    """Get recent signals from a strategy."""
    return []


@router.post("/{strategy_id}/pause")
async def pause_strategy(strategy_id: str):
    """Pause a running strategy."""
    return {"strategy_id": strategy_id, "action": "paused"}


@router.post("/{strategy_id}/resume")
async def resume_strategy(strategy_id: str):
    """Resume a paused strategy."""
    return {"strategy_id": strategy_id, "action": "resumed"}
