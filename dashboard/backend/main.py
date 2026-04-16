"""FastAPI application entry point for the QuantSystem dashboard.

Supports optional live trading runner integration:
  Set QS_LIVE_CONFIG env var to a YAML config path to auto-start a live
  trading runner within the backend process. The runner is then accessible
  via /api/live/runner/* endpoints and visible in the desktop app.

  Example:
    QS_LIVE_CONFIG=config/live/eth_sniper_live.yaml uvicorn dashboard.backend.main:app --port 8002
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.logging import get_logger, setup_logging
from core.paths import ensure_user_dir_scaffolded, get_env_path
from dashboard.backend.routers import backtest, data_layer, deployments, live_trading, logs as logs_router, market_data, paper_trading, portfolio, reports, risk, strategies, system, workshop

logger = get_logger(__name__)

# Background task handle for the live runner
_runner_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup/shutdown lifecycle.

    If QS_LIVE_CONFIG is set, starts a LiveTradingRunner in the background
    and registers it with the live_trading router for API access.
    """
    global _runner_task

    # Ensure user data dir exists and seed defaults on first run.
    ensure_user_dir_scaffolded()

    # Configure rotating file logging now that the logs dir exists.
    setup_logging(level="INFO", json_format=False)

    logger.info("dashboard_backend_starting")

    # Always load user's .env if present (API keys, recipient addresses)
    try:
        from dotenv import load_dotenv
        user_env = get_env_path()
        if user_env.exists():
            load_dotenv(user_env, override=False)
            logger.info("env_loaded", path=str(user_env))
    except ImportError:
        pass

    runner = None
    live_config = os.environ.get("QS_LIVE_CONFIG", "")

    if live_config:
        from pathlib import Path
        config_path = Path(live_config)
        if config_path.exists():
            logger.info("live_runner_auto_start", config=str(config_path))
            try:

                from core.live_trading_runner import LiveTradingRunner
                runner = LiveTradingRunner(str(config_path))
                await runner.setup()
                await runner.warmup()
                await runner.sync_exchange_positions()

                # Register with the API router
                live_trading.set_live_runner(runner)
                logger.info("live_runner_registered", strategy=runner._config.get("strategy_id"))

                # Start the trading loop in background
                _runner_task = asyncio.create_task(runner.run())
                logger.info("live_runner_started_background")
            except Exception as e:
                logger.error("live_runner_start_failed", error=str(e))
                runner = None
        else:
            logger.warning("live_config_not_found", path=str(config_path))

    # Load cloud deployments registry
    deployments.load_deployments()

    # Start daily report scheduler (fires at configured UTC time)
    from core.services.report_scheduler import get_scheduler
    scheduler = get_scheduler()
    try:
        await scheduler.start()
        logger.info("daily_report_scheduler_started", status=scheduler.status())
    except Exception as e:
        logger.error("daily_report_scheduler_start_failed", error=str(e))

    logger.info("dashboard_backend_ready")

    yield

    # Shutdown
    logger.info("dashboard_backend_stopping")
    try:
        await scheduler.stop()
    except Exception as e:
        logger.warning("daily_report_scheduler_stop_failed", error=str(e))
    if runner:
        logger.info("live_runner_shutting_down")
        await runner.shutdown(close_positions=False)
    if _runner_task and not _runner_task.done():
        _runner_task.cancel()
        try:
            await _runner_task
        except (asyncio.CancelledError, Exception):
            pass


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
app.include_router(workshop.router, prefix="/api/workshop", tags=["Workshop"])
app.include_router(data_layer.router, prefix="/api/data", tags=["Data Layer"])
app.include_router(deployments.router, tags=["Cloud Deployments"])
app.include_router(reports.router, tags=["Reports"])
app.include_router(logs_router.router, tags=["Logs"])


@app.get("/")
async def root():
    return {"service": "QuantSystem Dashboard API", "version": "0.1.0", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "healthy"}
