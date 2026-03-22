#!/usr/bin/env python3
"""Database initialization script for TimescaleDB.

Reads SQL migration files from data/storage/migrations/ in lexicographic order
and executes them against the configured TimescaleDB instance.

Environment variables (with defaults for local Docker):
    TIMESCALE_HOST      (default: localhost)
    TIMESCALE_PORT      (default: 5432)
    TIMESCALE_DB        (default: quantsystem)
    TIMESCALE_USER      (default: quant)
    TIMESCALE_PASSWORD  (default: quant)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import asyncpg

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "data" / "storage" / "migrations"

DB_CONFIG = {
    "host": os.getenv("TIMESCALE_HOST", "localhost"),
    "port": int(os.getenv("TIMESCALE_PORT", "5432")),
    "database": os.getenv("TIMESCALE_DB", "quantsystem"),
    "user": os.getenv("TIMESCALE_USER", "quant"),
    "password": os.getenv("TIMESCALE_PASSWORD", "quant"),
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("init_db")

# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _collect_migrations(directory: Path) -> list[Path]:
    """Return sorted list of .sql files in *directory*."""
    if not directory.is_dir():
        logger.error("Migrations directory does not exist: %s", directory)
        sys.exit(1)

    sql_files = sorted(directory.glob("*.sql"))
    if not sql_files:
        logger.warning("No SQL migration files found in %s", directory)
    return sql_files


async def _apply_migrations(conn: asyncpg.Connection, migrations: list[Path]) -> None:
    """Execute each migration file inside a transaction."""
    for migration in migrations:
        logger.info("Applying migration: %s", migration.name)
        sql = migration.read_text(encoding="utf-8")
        try:
            await conn.execute(sql)
            logger.info("  -> OK")
        except asyncpg.PostgresError as exc:
            logger.error("  -> FAILED: %s", exc)
            raise


async def _run() -> None:
    """Connect to TimescaleDB and execute all migrations."""
    migrations = _collect_migrations(MIGRATIONS_DIR)
    if not migrations:
        return

    logger.info(
        "Connecting to TimescaleDB at %s:%s/%s as %s",
        DB_CONFIG["host"],
        DB_CONFIG["port"],
        DB_CONFIG["database"],
        DB_CONFIG["user"],
    )

    try:
        conn: asyncpg.Connection = await asyncpg.connect(**DB_CONFIG)
    except (asyncpg.PostgresError, OSError) as exc:
        logger.error("Failed to connect to database: %s", exc)
        sys.exit(1)

    try:
        logger.info("Found %d migration(s) to apply", len(migrations))
        await _apply_migrations(conn, migrations)
        logger.info("All migrations applied successfully")
    finally:
        await conn.close()


def main() -> None:
    """Entry-point."""
    asyncio.run(_run())


if __name__ == "__main__":
    main()
