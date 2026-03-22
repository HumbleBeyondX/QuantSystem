"""MinIO/S3 Parquet storage for historical cold data.

Data is partitioned by: {asset_class}/{symbol}/{data_type}/{year}/{month}.parquet
Uses PyArrow for efficient Parquet read/write with Snappy compression.
"""

from __future__ import annotations

from io import BytesIO
from typing import List, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from minio import Minio

from core.config import settings
from core.logging import get_logger

logger = get_logger(__name__)


class ParquetStore:
    """Cold storage for historical data on MinIO (S3-compatible)."""

    def __init__(self, bucket: str = "historical-data") -> None:
        self._client = Minio(
            endpoint=settings.minio.endpoint,
            access_key=settings.minio.access_key,
            secret_key=settings.minio.secret_key,
            secure=settings.minio.secure,
        )
        self._bucket = bucket

    def _ensure_bucket(self) -> None:
        """Create bucket if it doesn't exist."""
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)
            logger.info("bucket_created", bucket=self._bucket)

    def _make_key(
        self, asset_class: str, symbol: str, data_type: str, year: int, month: int
    ) -> str:
        """Generate partitioned object key.

        Example: crypto/BTCUSDT/ohlcv/2024/03.parquet
        """
        # Sanitize symbol (BTC/USDT -> BTCUSDT)
        safe_symbol = symbol.replace("/", "")
        return f"{asset_class}/{safe_symbol}/{data_type}/{year}/{month:02d}.parquet"

    def write(
        self,
        df: pd.DataFrame,
        asset_class: str,
        symbol: str,
        data_type: str,
        year: int,
        month: int,
    ) -> str:
        """Write DataFrame as Parquet to MinIO.

        Returns the object key.
        """
        self._ensure_bucket()
        table = pa.Table.from_pandas(df)
        buffer = BytesIO()
        pq.write_table(table, buffer, compression="snappy")
        buffer.seek(0)

        key = self._make_key(asset_class, symbol, data_type, year, month)
        self._client.put_object(
            self._bucket,
            key,
            buffer,
            length=buffer.getbuffer().nbytes,
            content_type="application/octet-stream",
        )
        logger.info("parquet_written", key=key, rows=len(df))
        return key

    def read(
        self,
        asset_class: str,
        symbol: str,
        data_type: str,
        year: int,
        month: int,
    ) -> Optional[pd.DataFrame]:
        """Read Parquet file from MinIO into DataFrame."""
        key = self._make_key(asset_class, symbol, data_type, year, month)
        try:
            response = self._client.get_object(self._bucket, key)
            buffer = BytesIO(response.read())
            response.close()
            response.release_conn()
            table = pq.read_table(buffer)
            logger.debug("parquet_read", key=key, rows=table.num_rows)
            return table.to_pandas()
        except Exception as e:
            logger.warning("parquet_read_failed", key=key, error=str(e))
            return None

    def read_range(
        self,
        asset_class: str,
        symbol: str,
        data_type: str,
        start_year: int,
        start_month: int,
        end_year: int,
        end_month: int,
    ) -> pd.DataFrame:
        """Read multiple monthly Parquet files and concatenate."""
        frames: List[pd.DataFrame] = []
        year, month = start_year, start_month
        while (year, month) <= (end_year, end_month):
            df = self.read(asset_class, symbol, data_type, year, month)
            if df is not None:
                frames.append(df)
            month += 1
            if month > 12:
                month = 1
                year += 1

        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def list_objects(
        self, asset_class: str, symbol: str, data_type: str
    ) -> List[str]:
        """List all available Parquet files for a symbol."""
        safe_symbol = symbol.replace("/", "")
        prefix = f"{asset_class}/{safe_symbol}/{data_type}/"
        objects = self._client.list_objects(self._bucket, prefix=prefix, recursive=True)
        return [obj.object_name for obj in objects]

    def health_check(self) -> bool:
        """Check MinIO connectivity."""
        try:
            self._client.list_buckets()
            return True
        except Exception:
            return False
