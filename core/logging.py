"""Structured logging setup using structlog.

Provides consistent, JSON-formatted logging across all modules.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog


def setup_logging(level: str = "INFO", json_format: bool = True) -> None:
    """Configure structured logging for the entire application.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        json_format: If True, output JSON lines. If False, human-readable console output.
    """
    # Shared processors
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if json_format:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, level.upper()))

    # Silence noisy libraries
    for lib in ("aiokafka", "asyncpg", "ccxt", "urllib3", "aiohttp"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def get_logger(name: str = "") -> structlog.stdlib.BoundLogger:
    """Get a structured logger instance.

    Usage:
        from core.logging import get_logger
        logger = get_logger(__name__)
        logger.info("order_placed", symbol="AAPL", quantity=100)
    """
    return structlog.get_logger(name)
