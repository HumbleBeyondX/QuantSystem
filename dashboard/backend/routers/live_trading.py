"""Live Trading API router — exposes orchestrator controls to the frontend.

Endpoints:
    GET  /api/live/status              — All strategy states + orchestrator health
    POST /api/live/start/{strategy_id} — Start a strategy
    POST /api/live/stop/{strategy_id}  — Graceful stop
    POST /api/live/pause/{strategy_id} — Pause (keep positions)
    POST /api/live/resume/{strategy_id} — Resume
    POST /api/live/force-close/{strategy_id} — Emergency flatten positions
    POST /api/live/emergency-stop      — KILL SWITCH: stop everything
    POST /api/live/reset-emergency     — Clear emergency stop flag
    GET  /api/live/positions/{strategy_id} — Current positions for a strategy
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/live", tags=["live-trading"])

# ------------------------------------------------------------------
# Module-level orchestrator reference (set by main.py at startup)
# ------------------------------------------------------------------

_orchestrator = None


def set_orchestrator(orchestrator) -> None:
    """Called by main.py to inject the orchestrator singleton."""
    global _orchestrator
    _orchestrator = orchestrator


def _get_orchestrator():
    if _orchestrator is None:
        raise HTTPException(503, "Orchestrator not initialized")
    return _orchestrator


# ------------------------------------------------------------------
# Request / Response models
# ------------------------------------------------------------------

class StartRequest(BaseModel):
    """Optional overrides when starting a strategy."""
    config: Optional[Dict[str, Any]] = None


class ActionResponse(BaseModel):
    status: str
    message: str
    data: Optional[Dict[str, Any]] = None


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@router.get("/status")
async def get_live_status():
    """Get orchestrator status and all strategy states."""
    orch = _get_orchestrator()
    return orch.get_status()


@router.get("/status/{strategy_id}")
async def get_strategy_status(strategy_id: str):
    """Get status of a single strategy."""
    orch = _get_orchestrator()
    try:
        return orch.get_strategy_status(strategy_id)
    except KeyError:
        raise HTTPException(404, f"Strategy not found: {strategy_id}")


@router.post("/start/{strategy_id}", response_model=ActionResponse)
async def start_strategy(strategy_id: str, body: Optional[StartRequest] = None):
    """Start a registered strategy."""
    orch = _get_orchestrator()
    try:
        await orch.start_strategy(strategy_id)
        return ActionResponse(
            status="ok",
            message=f"Strategy {strategy_id} started",
            data=orch.get_strategy_status(strategy_id),
        )
    except KeyError:
        raise HTTPException(404, f"Strategy not found: {strategy_id}")
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        logger.error("start_strategy_error", strategy_id=strategy_id, error=str(e))
        raise HTTPException(500, str(e))


@router.post("/stop/{strategy_id}", response_model=ActionResponse)
async def stop_strategy(strategy_id: str):
    """Gracefully stop a strategy."""
    orch = _get_orchestrator()
    try:
        await orch.stop_strategy(strategy_id)
        return ActionResponse(
            status="ok",
            message=f"Strategy {strategy_id} stopped",
            data=orch.get_strategy_status(strategy_id),
        )
    except KeyError:
        raise HTTPException(404, f"Strategy not found: {strategy_id}")
    except Exception as e:
        logger.error("stop_strategy_error", strategy_id=strategy_id, error=str(e))
        raise HTTPException(500, str(e))


@router.post("/pause/{strategy_id}", response_model=ActionResponse)
async def pause_strategy(strategy_id: str):
    """Pause a running strategy (keeps positions)."""
    orch = _get_orchestrator()
    try:
        await orch.pause_strategy(strategy_id)
        return ActionResponse(
            status="ok",
            message=f"Strategy {strategy_id} paused",
            data=orch.get_strategy_status(strategy_id),
        )
    except KeyError:
        raise HTTPException(404, f"Strategy not found: {strategy_id}")


@router.post("/resume/{strategy_id}", response_model=ActionResponse)
async def resume_strategy(strategy_id: str):
    """Resume a paused strategy."""
    orch = _get_orchestrator()
    try:
        await orch.resume_strategy(strategy_id)
        return ActionResponse(
            status="ok",
            message=f"Strategy {strategy_id} resumed",
            data=orch.get_strategy_status(strategy_id),
        )
    except KeyError:
        raise HTTPException(404, f"Strategy not found: {strategy_id}")


@router.post("/force-close/{strategy_id}", response_model=ActionResponse)
async def force_close_positions(strategy_id: str):
    """Emergency: close all positions for a strategy via market orders."""
    orch = _get_orchestrator()
    try:
        result = await orch.force_close_positions(strategy_id)
        return ActionResponse(
            status="ok",
            message=f"Force-closed positions for {strategy_id}",
            data=result,
        )
    except KeyError:
        raise HTTPException(404, f"Strategy not found: {strategy_id}")
    except Exception as e:
        logger.error("force_close_error", strategy_id=strategy_id, error=str(e))
        raise HTTPException(500, str(e))


@router.post("/emergency-stop", response_model=ActionResponse)
async def emergency_stop():
    """KILL SWITCH: Stop all strategies and close all positions."""
    orch = _get_orchestrator()
    logger.critical("emergency_stop_api_called")
    result = await orch.emergency_stop()
    return ActionResponse(
        status="ok",
        message="Emergency stop executed — all strategies stopped",
        data=result,
    )


@router.post("/reset-emergency", response_model=ActionResponse)
async def reset_emergency():
    """Clear emergency stop flag to allow restarting strategies."""
    orch = _get_orchestrator()
    await orch.reset_emergency_stop()
    return ActionResponse(
        status="ok",
        message="Emergency stop cleared — strategies can be restarted",
    )


@router.get("/positions/{strategy_id}")
async def get_positions(strategy_id: str):
    """Get current positions for a strategy from the execution engine."""
    orch = _get_orchestrator()
    try:
        slot = orch._get_slot(strategy_id)
    except KeyError:
        raise HTTPException(404, f"Strategy not found: {strategy_id}")

    engine = orch._order_router.get_engine(slot.asset_class, name=slot.exchange)
    if not engine:
        return {"strategy_id": strategy_id, "positions": {}}

    try:
        positions = await engine.get_positions()
        return {"strategy_id": strategy_id, "positions": positions}
    except Exception as e:
        logger.error("get_positions_error", strategy_id=strategy_id, error=str(e))
        return {"strategy_id": strategy_id, "positions": {}, "error": str(e)}
