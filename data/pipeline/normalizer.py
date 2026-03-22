"""Data normalizer: converts raw connector output to internal domain models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from core.enums import AssetClass, OrderSide, TimeFrame
from core.logging import get_logger
from core.models import Bar, OrderBookLevel, OrderBookSnapshot, Tick, Trade

logger = get_logger(__name__)


class DataNormalizer:
    """Normalizes raw data from various connectors into unified domain models."""

    @staticmethod
    def normalize_tick(raw: Dict[str, Any]) -> Optional[Tick]:
        """Convert raw tick/quote dict to Tick model."""
        try:
            return Tick(
                symbol=raw["symbol"],
                timestamp=_parse_timestamp(raw.get("timestamp")),
                bid=float(raw.get("bid", 0)),
                ask=float(raw.get("ask", 0)),
                last=float(raw.get("last", raw.get("price", 0))),
                volume=float(raw.get("volume", raw.get("amount", 0))),
                source=raw.get("source", ""),
                asset_class=_parse_asset_class(raw.get("source", "")),
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("normalize_tick_failed", error=str(e), raw_keys=list(raw.keys()))
            return None

    @staticmethod
    def normalize_trade(raw: Dict[str, Any]) -> Optional[Trade]:
        """Convert raw trade dict to Trade model."""
        try:
            side_str = raw.get("side", "").upper()
            side = OrderSide.BUY if side_str in ("BUY", "B") else OrderSide.SELL
            return Trade(
                symbol=raw["symbol"],
                timestamp=_parse_timestamp(raw.get("timestamp")),
                price=float(raw["price"]),
                amount=float(raw.get("amount", raw.get("quantity", 0))),
                side=side,
                trade_id=str(raw.get("trade_id", "")),
                source=raw.get("source", ""),
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("normalize_trade_failed", error=str(e))
            return None

    @staticmethod
    def normalize_bar(raw: Dict[str, Any]) -> Optional[Bar]:
        """Convert raw OHLCV dict to Bar model."""
        try:
            tf_str = raw.get("timeframe", "1m")
            timeframe = _parse_timeframe(tf_str)
            return Bar(
                symbol=raw["symbol"],
                timestamp=_parse_timestamp(raw.get("timestamp")),
                open=float(raw["open"]),
                high=float(raw["high"]),
                low=float(raw["low"]),
                close=float(raw["close"]),
                volume=float(raw.get("volume", 0)),
                timeframe=timeframe,
                vwap=float(raw["vwap"]) if raw.get("vwap") else None,
                source=raw.get("source", ""),
                asset_class=_parse_asset_class(raw.get("source", "")),
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("normalize_bar_failed", error=str(e))
            return None

    @staticmethod
    def normalize_orderbook(raw: Dict[str, Any]) -> Optional[OrderBookSnapshot]:
        """Convert raw orderbook dict to OrderBookSnapshot model."""
        try:
            bids = [
                OrderBookLevel(price=float(b[0]), quantity=float(b[1]))
                for b in raw.get("bids", [])
            ]
            asks = [
                OrderBookLevel(price=float(a[0]), quantity=float(a[1]))
                for a in raw.get("asks", [])
            ]
            return OrderBookSnapshot(
                symbol=raw["symbol"],
                timestamp=_parse_timestamp(raw.get("timestamp")),
                bids=bids,
                asks=asks,
                source=raw.get("source", ""),
            )
        except (KeyError, ValueError, TypeError, IndexError) as e:
            logger.warning("normalize_orderbook_failed", error=str(e))
            return None


# ============================================================
# Helper functions
# ============================================================

def _parse_timestamp(value: Any) -> datetime:
    """Parse timestamp from various formats."""
    if value is None:
        return datetime.utcnow()
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        # Milliseconds epoch
        if value > 1e12:
            return datetime.utcfromtimestamp(value / 1000)
        return datetime.utcfromtimestamp(value)
    if isinstance(value, str):
        # Try ISO format first
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
        # Try common formats
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
    return datetime.utcnow()


def _parse_asset_class(source: str) -> AssetClass:
    """Infer asset class from data source name."""
    source_lower = source.lower()
    if source_lower in ("binance", "okx", "bybit", "dydx", "hyperliquid"):
        return AssetClass.CRYPTO
    if source_lower in ("ib", "interactive_brokers", "polygon"):
        return AssetClass.EQUITY
    return AssetClass.CRYPTO


def _parse_timeframe(tf: str) -> TimeFrame:
    """Parse timeframe string to enum."""
    mapping = {
        "1m": TimeFrame.M1,
        "5m": TimeFrame.M5,
        "15m": TimeFrame.M15,
        "30m": TimeFrame.M30,
        "1h": TimeFrame.H1,
        "4h": TimeFrame.H4,
        "1d": TimeFrame.D1,
        "1w": TimeFrame.W1,
    }
    return mapping.get(tf.lower(), TimeFrame.M1)
