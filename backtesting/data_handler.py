"""Data handler for backtesting: loads data from various sources."""

from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from core.logging import get_logger
from data.storage.parquet_store import ParquetStore
from data.storage.timescale import TimescaleClient

logger = get_logger(__name__)


class BacktestDataHandler:
    """Loads and prepares historical data for backtesting."""

    def __init__(
        self,
        parquet_store: Optional[ParquetStore] = None,
        timescale_client: Optional[TimescaleClient] = None,
    ) -> None:
        self._parquet = parquet_store
        self._timescale = timescale_client

    def load_from_csv(self, path: str, symbol: str = "") -> pd.DataFrame:
        """Load OHLCV data from a CSV file."""
        df = pd.read_csv(path, parse_dates=True)
        # Standardize column names
        col_map = {
            "Date": "timestamp", "date": "timestamp", "Time": "timestamp",
            "Open": "open", "High": "high", "Low": "low", "Close": "close",
            "Volume": "volume", "Vol": "volume",
        }
        df.rename(columns={k: v for k, v in col_map.items() if k in df.columns}, inplace=True)
        logger.info("data_loaded_csv", path=path, rows=len(df))
        return df

    def load_from_parquet(
        self,
        asset_class: str,
        symbol: str,
        data_type: str,
        start_year: int,
        start_month: int,
        end_year: int,
        end_month: int,
    ) -> pd.DataFrame:
        """Load data from MinIO Parquet store."""
        if not self._parquet:
            raise RuntimeError("ParquetStore not configured")
        return self._parquet.read_range(
            asset_class, symbol, data_type,
            start_year, start_month, end_year, end_month,
        )

    def load_multi_symbol(
        self, paths: Dict[str, str]
    ) -> Dict[str, pd.DataFrame]:
        """Load data for multiple symbols from CSV files."""
        return {symbol: self.load_from_csv(path, symbol) for symbol, path in paths.items()}
