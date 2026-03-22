"""Message schemas for Kafka topics.

Defines the expected structure of messages flowing through the system.
Used for validation and documentation.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# ============================================================
# Market Data Schemas
# ============================================================

TICK_SCHEMA = {
    "source": str,        # "ib" | "polygon" | "binance" | ...
    "symbol": str,        # "AAPL" | "BTC/USDT"
    "data_type": str,     # "tick"
    "bid": float,
    "ask": float,
    "last": float,
    "volume": float,
    "timestamp": str,     # ISO 8601
}

TRADE_SCHEMA = {
    "source": str,
    "symbol": str,
    "data_type": str,     # "trade"
    "price": float,
    "amount": float,
    "side": str,          # "BUY" | "SELL"
    "trade_id": str,
    "timestamp": str,
}

ORDERBOOK_SCHEMA = {
    "source": str,
    "symbol": str,
    "data_type": str,     # "orderbook"
    "bids": list,         # [[price, qty], ...]
    "asks": list,
    "timestamp": str,
}

BAR_SCHEMA = {
    "source": str,
    "symbol": str,
    "data_type": str,     # "bar" | "ohlcv"
    "timeframe": str,     # "1m" | "5m" | "1h" | "1d"
    "open": float,
    "high": float,
    "low": float,
    "close": float,
    "volume": float,
    "vwap": float,        # Optional
    "timestamp": str,
}


def validate_message(message: Dict[str, Any], schema: Dict[str, type]) -> bool:
    """Basic schema validation for Kafka messages."""
    for key, expected_type in schema.items():
        if key not in message:
            return False
        if not isinstance(message[key], expected_type):
            # Allow None for optional fields
            if message[key] is not None:
                return False
    return True
