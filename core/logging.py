"""Structured logging setup using structlog.

Provides consistent logging across all modules.

Outputs:
  - Console (stdout) — human-readable for dev, JSON for production
  - Rotating file (``<user_data>/logs/backend-YYYY-MM-DD.log``) — always JSON
    so it's machine-parseable for the log viewer.

File output is always enabled (the log viewer depends on it). Rotation:
14 days kept, new file each UTC midnight.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from typing import Any

import structlog


_logging_configured: bool = False


def setup_logging(level: str = "INFO", json_format: bool = True) -> None:
    """Configure structured logging for the entire application.

    Idempotent — safe to call multiple times. Only the first call configures
    the root logger; subsequent calls are no-ops.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        json_format: If True, stdout output uses JSON lines. If False, a
                     human-readable console renderer. File output is always
                     JSON regardless of this flag.
    """
    global _logging_configured
    if _logging_configured:
        return

    # Shared processors for all handlers
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # ── Console handler ──
    console_renderer = (
        structlog.processors.JSONRenderer()
        if json_format
        else structlog.dev.ConsoleRenderer(colors=True)
    )
    console_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            console_renderer,
        ],
        foreign_pre_chain=shared_processors,
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_formatter)

    # ── File handler (rotating, always JSON) ──
    file_handler = None
    try:
        # Local import avoids a potential cycle during module load.
        from core.paths import get_logs_dir

        log_path = get_logs_dir() / "backend.log"
        file_handler = logging.handlers.TimedRotatingFileHandler(
            filename=str(log_path),
            when="midnight",
            interval=1,
            backupCount=14,
            encoding="utf-8",
            utc=True,
        )
        # Rotated file suffix: backend.log.2026-04-15
        file_handler.suffix = "%Y-%m-%d"
        file_formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
            foreign_pre_chain=shared_processors,
        )
        file_handler.setFormatter(file_formatter)
    except Exception:
        # Non-fatal: if the logs dir can't be created (e.g. read-only FS),
        # just skip file logging. Console still works.
        file_handler = None

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    if file_handler is not None:
        root_logger.addHandler(file_handler)
    root_logger.setLevel(getattr(logging, level.upper()))

    # Silence noisy libraries
    for lib in ("aiokafka", "asyncpg", "ccxt", "urllib3", "aiohttp",
                "httpx", "httpcore", "watchfiles", "googleapiclient.discovery_cache"):
        logging.getLogger(lib).setLevel(logging.WARNING)

    _logging_configured = True


def get_logger(name: str = "") -> structlog.stdlib.BoundLogger:
    """Get a structured logger instance.

    Usage:
        from core.logging import get_logger
        logger = get_logger(__name__)
        logger.info("order_placed", symbol="AAPL", quantity=100)
    """
    return structlog.get_logger(name)
