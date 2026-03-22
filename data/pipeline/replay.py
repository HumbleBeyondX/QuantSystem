"""Historical data replay: stream Parquet data through Kafka for backtesting.

Replays historical data at configurable speed through the same Kafka pipeline
used in live trading, enabling realistic integration testing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd

from core.enums import TimeFrame
from core.logging import get_logger
from data.storage.parquet_store import ParquetStore
from data.storage.timescale import TimescaleClient
from data.streaming.producer import KafkaProducerWrapper

logger = get_logger(__name__)


class HistoricalReplay:
    """Replay historical data through Kafka at configurable speed."""

    def __init__(
        self,
        kafka_producer: KafkaProducerWrapper,
        parquet_store: Optional[ParquetStore] = None,
        timescale_client: Optional[TimescaleClient] = None,
    ) -> None:
        self._producer = kafka_producer
        self._parquet = parquet_store
        self._timescale = timescale_client
        self._running = False

    async def replay_from_parquet(
        self,
        asset_class: str,
        symbol: str,
        data_type: str,
        start_year: int,
        start_month: int,
        end_year: int,
        end_month: int,
        speed_multiplier: float = 1.0,
    ) -> Dict[str, int]:
        """Replay Parquet historical data through Kafka.

        Args:
            speed_multiplier: 1.0 = real-time, 10.0 = 10x speed, 0 = as fast as possible
        """
        if not self._parquet:
            raise RuntimeError("ParquetStore not configured")

        df = self._parquet.read_range(
            asset_class, symbol, data_type,
            start_year, start_month, end_year, end_month,
        )

        if df.empty:
            logger.warning("replay_no_data", symbol=symbol)
            return {"sent": 0, "errors": 0}

        return await self._replay_dataframe(df, asset_class, symbol, data_type, speed_multiplier)

    async def replay_from_timescale(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        data_type: str = "ohlcv",
        speed_multiplier: float = 1.0,
    ) -> Dict[str, int]:
        """Replay data from TimescaleDB through Kafka."""
        if not self._timescale:
            raise RuntimeError("TimescaleClient not configured")

        df = await self._timescale.query_bars(symbol, start, end)
        if df.empty:
            logger.warning("replay_no_data", symbol=symbol)
            return {"sent": 0, "errors": 0}

        asset_class = "equity" if "/" not in symbol else "crypto"
        return await self._replay_dataframe(df, asset_class, symbol, data_type, speed_multiplier)

    async def _replay_dataframe(
        self,
        df: pd.DataFrame,
        asset_class: str,
        symbol: str,
        data_type: str,
        speed_multiplier: float,
    ) -> Dict[str, int]:
        """Core replay logic: iterate DataFrame rows and publish to Kafka."""
        self._running = True
        sent = 0
        errors = 0
        safe_symbol = symbol.replace("/", "")
        topic = f"{asset_class}.{safe_symbol}.{data_type}"

        # Sort by timestamp
        ts_col = None
        for col in ("timestamp", "time", "datetime", "date"):
            if col in df.columns:
                ts_col = col
                break
        if ts_col:
            df = df.sort_values(ts_col).reset_index(drop=True)

        logger.info("replay_starting", topic=topic, rows=len(df), speed=speed_multiplier)

        prev_ts = None
        for idx, row in df.iterrows():
            if not self._running:
                break

            # Simulate timing delay
            if speed_multiplier > 0 and ts_col and prev_ts is not None:
                current_ts = pd.Timestamp(row[ts_col])
                prev_timestamp = pd.Timestamp(prev_ts)
                if pd.notna(current_ts) and pd.notna(prev_timestamp):
                    delta_sec = (current_ts - prev_timestamp).total_seconds()
                    if delta_sec > 0:
                        await asyncio.sleep(delta_sec / speed_multiplier)

            # Build message
            message = self._row_to_message(row, symbol, data_type, asset_class)

            try:
                await self._producer.send(topic, message)
                sent += 1
            except Exception as e:
                errors += 1
                if errors <= 5:
                    logger.warning("replay_send_error", error=str(e))

            if ts_col:
                prev_ts = row[ts_col]

        logger.info("replay_complete", topic=topic, sent=sent, errors=errors)
        return {"sent": sent, "errors": errors}

    @staticmethod
    def _row_to_message(
        row: pd.Series, symbol: str, data_type: str, asset_class: str
    ) -> Dict[str, Any]:
        """Convert a DataFrame row to a Kafka message dict."""
        msg: Dict[str, Any] = {
            "source": "replay",
            "symbol": symbol,
            "data_type": data_type,
            "asset_class": asset_class,
        }

        # Map common column names
        col_map = {
            "open": "open", "high": "high", "low": "low", "close": "close",
            "volume": "volume", "vwap": "vwap",
            "bid": "bid", "ask": "ask", "last": "last",
            "price": "price", "amount": "amount", "side": "side",
        }

        for col, key in col_map.items():
            if col in row.index and pd.notna(row[col]):
                msg[key] = float(row[col]) if isinstance(row[col], (int, float)) else str(row[col])

        # Timestamp
        for ts_col in ("timestamp", "time", "datetime", "date"):
            if ts_col in row.index:
                val = row[ts_col]
                msg["timestamp"] = val.isoformat() if hasattr(val, "isoformat") else str(val)
                break

        return msg

    def stop(self) -> None:
        """Stop replay."""
        self._running = False
