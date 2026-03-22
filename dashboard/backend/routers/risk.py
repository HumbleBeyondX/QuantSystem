"""Risk API endpoints: VaR, drawdown, Greeks exposure."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/summary")
async def get_risk_summary():
    """Get risk summary: VaR, drawdown, leverage."""
    return {
        "var_95": 0, "cvar_95": 0, "drawdown_pct": 0,
        "leverage": 0, "is_halted": False,
    }


@router.get("/greeks")
async def get_greeks_exposure():
    """Get aggregate Greeks exposure."""
    return {"delta": 0, "gamma": 0, "vega": 0, "theta": 0}


@router.post("/circuit-breaker/reset")
async def reset_circuit_breaker():
    """Manually reset the circuit breaker."""
    return {"status": "reset"}
