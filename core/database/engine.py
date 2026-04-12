"""Async database engine factory for QuantSystem.

Supports two backends:
- SQLite via aiosqlite (local, default)
- PostgreSQL via asyncpg (cloud / Supabase)

Usage:
    from core.database.engine import init_db, get_session

    await init_db()  # creates tables if needed
    async with get_session() as session:
        session.add(...)
        await session.commit()
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.database.models import Base

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level singletons (set by init_db)
# ---------------------------------------------------------------------------
_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


# ---------------------------------------------------------------------------
# Engine construction
# ---------------------------------------------------------------------------

def _default_sqlite_url() -> str:
    """Return the default SQLite URL, ensuring the directory exists."""
    db_dir = Path(os.environ.get("QS_DATA_DIR", Path(__file__).resolve().parents[2] / "data"))
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "quantsystem.db"
    return f"sqlite+aiosqlite:///{db_path}"


def build_engine(url: Optional[str] = None, *, echo: bool = False) -> AsyncEngine:
    """Create an async SQLAlchemy engine.

    Parameters
    ----------
    url : str, optional
        Database URL.  Defaults to ``QS_DATABASE_URL`` env var, falling back
        to a local SQLite file at ``data/quantsystem.db``.
    echo : bool
        If True, log all SQL statements (noisy, dev only).
    """
    if url is None:
        url = os.environ.get("QS_DATABASE_URL", _default_sqlite_url())

    connect_args: dict = {}
    pool_kwargs: dict = {}

    if url.startswith("sqlite"):
        # WAL mode for concurrent reads; foreign keys on
        connect_args = {"check_same_thread": False}
        pool_kwargs = {"pool_pre_ping": True}
    else:
        # PostgreSQL (asyncpg)
        pool_kwargs = {
            "pool_size": 5,
            "max_overflow": 10,
            "pool_pre_ping": True,
        }

    engine = create_async_engine(
        url,
        echo=echo,
        connect_args=connect_args,
        **pool_kwargs,
    )
    return engine


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

async def init_db(
    url: Optional[str] = None,
    *,
    echo: bool = False,
    create_tables: bool = True,
) -> AsyncEngine:
    """Initialize the database — creates engine, session factory, and tables.

    Safe to call multiple times; subsequent calls are no-ops unless the engine
    was previously shut down.
    """
    global _engine, _session_factory

    if _engine is not None:
        return _engine

    _engine = build_engine(url, echo=echo)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    if create_tables:
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Enable WAL mode for SQLite
            if str(_engine.url).startswith("sqlite"):
                await conn.execute(
                    __import__("sqlalchemy").text("PRAGMA journal_mode=WAL")
                )

    logger.info("Database initialised: %s", _engine.url)
    return _engine


async def shutdown_db() -> None:
    """Dispose the engine and clear singletons."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("Database engine disposed")
    _engine = None
    _session_factory = None


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def get_engine() -> AsyncEngine:
    """Return the current engine (raises if init_db() was not called)."""
    if _engine is None:
        raise RuntimeError("Database not initialised — call init_db() first")
    return _engine


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Provide a transactional async session scope.

    Usage::

        async with get_session() as session:
            session.add(record)
            await session.commit()
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialised — call init_db() first")

    session = _session_factory()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
