"""FastAPI application entry point for the QuantSystem dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.logging import get_logger
from dashboard.backend.routers import backtest, market_data, paper_trading, portfolio, risk, strategies, system

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown lifecycle."""
    logger.info("dashboard_backend_starting")
    # TODO: Initialize DB connections, Kafka consumers, etc.
    yield
    logger.info("dashboard_backend_stopping")


app = FastAPI(
    title="QuantSystem Dashboard API",
    description="API for the Production Quantitative Trading System",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(portfolio.router, prefix="/api/portfolio", tags=["Portfolio"])
app.include_router(strategies.router, prefix="/api/strategies", tags=["Strategies"])
app.include_router(backtest.router, prefix="/api/backtest", tags=["Backtest"])
app.include_router(market_data.router, prefix="/api/market", tags=["Market Data"])
app.include_router(risk.router, prefix="/api/risk", tags=["Risk"])
app.include_router(system.router, prefix="/api/system", tags=["System"])
app.include_router(paper_trading.router, prefix="/api/paper-trading", tags=["Paper Trading"])


@app.get("/")
async def root():
    return {"service": "QuantSystem Dashboard API", "version": "0.1.0", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "healthy"}
