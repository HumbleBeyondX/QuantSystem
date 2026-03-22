"""Portfolio API endpoints: P&L, positions, holdings, equity curve."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/summary")
async def get_portfolio_summary():
    """Get portfolio summary: total value, P&L, positions count."""
    # TODO: Connect to PositionManager
    return {
        "total_value": 100000,
        "cash": 50000,
        "unrealized_pnl": 0,
        "realized_pnl": 0,
        "daily_pnl": 0,
        "n_positions": 0,
        "leverage": 0,
    }


@router.get("/positions")
async def get_positions():
    """Get all current positions."""
    return []


@router.get("/equity-curve")
async def get_equity_curve(days: int = 30):
    """Get equity curve data for charting."""
    return []


@router.get("/top-performers")
async def get_top_performers():
    """Get top performing holdings."""
    return []
