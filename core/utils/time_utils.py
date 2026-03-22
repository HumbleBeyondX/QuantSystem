"""Timezone handling, market hours, and trading calendar utilities."""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

# Common timezones
UTC = ZoneInfo("UTC")
ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")

# US equity market hours (ET)
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)
PRE_MARKET_OPEN = time(4, 0)
AFTER_HOURS_CLOSE = time(20, 0)


def now_utc() -> datetime:
    """Current UTC timestamp."""
    return datetime.now(UTC)


def now_et() -> datetime:
    """Current Eastern Time timestamp."""
    return datetime.now(ET)


def to_utc(dt: datetime) -> datetime:
    """Convert any datetime to UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def to_et(dt: datetime) -> datetime:
    """Convert any datetime to Eastern Time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ET)


def is_market_open(dt: Optional[datetime] = None) -> bool:
    """Check if US equity markets are currently open (regular hours)."""
    if dt is None:
        dt = now_et()
    else:
        dt = to_et(dt)

    # Weekend check
    if dt.weekday() >= 5:
        return False

    current_time = dt.time()
    return MARKET_OPEN <= current_time < MARKET_CLOSE


def is_crypto_market_open() -> bool:
    """Crypto markets are always open (24/7/365)."""
    return True


def ms_to_datetime(ms: int) -> datetime:
    """Convert millisecond epoch timestamp to UTC datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def datetime_to_ms(dt: datetime) -> int:
    """Convert datetime to millisecond epoch timestamp."""
    return int(dt.timestamp() * 1000)
