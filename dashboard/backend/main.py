"""FastAPI application entry point for the QuantSystem dashboard."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.logging import get_logger
from dashboard.backend.routers import backtest, live_trading, market_data, paper_trading, portfolio, risk, strategies, system

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown lifecycle."""
    logger.info("dashboard_backend_starting")

    # Initialize database
    try:
        from core.database.engine import init_db
        await init_db()
        logger.info("database_initialized")
    except Exception as e:
        logger.warning("database_init_skipped", error=str(e))

    # Initialize orchestrator (lazy — strategies registered elsewhere)
    try:
        from core.events import EventBus
        from core.orchestrator import TradingOrchestrator
        from execution.oms.order_router import OrderRouter

        event_bus = EventBus()
        order_router = OrderRouter()
        orchestrator = TradingOrchestrator(event_bus, order_router)
        await orchestrator.start()
        live_trading.set_orchestrator(orchestrator)
        app.state.orchestrator = orchestrator
        app.state.event_bus = event_bus
        app.state.order_router = order_router
        logger.info("orchestrator_initialized")
    except Exception as e:
        logger.warning("orchestrator_init_skipped", error=str(e))

    yield

    # Shutdown
    if hasattr(app.state, "orchestrator"):
        await app.state.orchestrator.shutdown()

    try:
        from core.database.engine import shutdown_db
        await shutdown_db()
    except Exception:
        pass

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
    allow_origins=["*"],
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
app.include_router(live_trading.router, tags=["Live Trading"])


@app.get("/")
async def root():
    return {"service": "QuantSystem Dashboard API", "version": "0.1.0", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "healthy"}
