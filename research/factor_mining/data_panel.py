"""DataPanel — multi-asset OHLCV wrapper for factor mining.

Wraps a long-format DataFrame (timestamp, ticker, OHLCV) and exposes
wide-format projections that factors operate on. All wide DataFrames
are indexed by date with columns = tickers.

Design contract:
  * Input long DataFrame: columns ['timestamp', 'ticker', 'open',
                                    'high', 'low', 'close', 'volume']
  * Output wide DataFrames: index = date, columns = tickers, values =
                            specific OHLCV field.
  * Both index and columns are ALIGNED across .closes_wide,
    .returns_wide, .volume_wide etc. — this is a hard invariant
    that downstream operators rely on.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd


class DataPanel:
    """Multi-asset OHLCV panel."""

    def __init__(self, long_df: pd.DataFrame):
        required = {"timestamp", "ticker", "open", "high", "low", "close", "volume"}
        missing = required - set(long_df.columns)
        if missing:
            raise ValueError(f"long_df is missing columns: {missing}")

        long_df = long_df.copy()
        long_df["timestamp"] = pd.to_datetime(long_df["timestamp"])
        if hasattr(long_df["timestamp"].dt, "tz") and long_df["timestamp"].dt.tz is not None:
            long_df["timestamp"] = long_df["timestamp"].dt.tz_localize(None)

        self._long = long_df.sort_values(["ticker", "timestamp"]).reset_index(drop=True)

        # Pre-compute wide projections (lazy — computed once, cached)
        self._closes: Optional[pd.DataFrame] = None
        self._opens: Optional[pd.DataFrame] = None
        self._highs: Optional[pd.DataFrame] = None
        self._lows: Optional[pd.DataFrame] = None
        self._volumes: Optional[pd.DataFrame] = None
        self._returns: Optional[pd.DataFrame] = None
        self._vwaps: Optional[pd.DataFrame] = None
        self._dollar_volumes: Optional[pd.DataFrame] = None

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------
    @classmethod
    def from_parquet(cls, path: str | Path) -> "DataPanel":
        df = pd.read_parquet(path)
        return cls(df)

    @classmethod
    def from_long_df(cls, df: pd.DataFrame) -> "DataPanel":
        return cls(df)

    # ------------------------------------------------------------------
    # Wide projections (cached)
    # ------------------------------------------------------------------
    def _pivot(self, value_col: str) -> pd.DataFrame:
        wide = self._long.pivot_table(
            index="timestamp", columns="ticker", values=value_col, aggfunc="last",
        )
        return wide.sort_index()

    @property
    def closes_wide(self) -> pd.DataFrame:
        if self._closes is None:
            self._closes = self._pivot("close")
        return self._closes

    @property
    def opens_wide(self) -> pd.DataFrame:
        if self._opens is None:
            self._opens = self._pivot("open")
        return self._opens

    @property
    def highs_wide(self) -> pd.DataFrame:
        if self._highs is None:
            self._highs = self._pivot("high")
        return self._highs

    @property
    def lows_wide(self) -> pd.DataFrame:
        if self._lows is None:
            self._lows = self._pivot("low")
        return self._lows

    @property
    def volumes_wide(self) -> pd.DataFrame:
        if self._volumes is None:
            self._volumes = self._pivot("volume")
        return self._volumes

    @property
    def returns_wide(self) -> pd.DataFrame:
        """Daily simple returns (close[t]/close[t-1] − 1)."""
        if self._returns is None:
            self._returns = self.closes_wide.pct_change()
        return self._returns

    @property
    def log_returns_wide(self) -> pd.DataFrame:
        """Daily log returns."""
        return np.log(self.closes_wide / self.closes_wide.shift(1))

    @property
    def dollar_volumes_wide(self) -> pd.DataFrame:
        """close × volume — proxy for liquidity."""
        if self._dollar_volumes is None:
            self._dollar_volumes = self.closes_wide * self.volumes_wide
        return self._dollar_volumes

    @property
    def vwaps_wide(self) -> pd.DataFrame:
        """Daily VWAP proxy: (high + low + close) / 3.
        True VWAP needs intraday data; for daily we use the typical price."""
        if self._vwaps is None:
            self._vwaps = (self.highs_wide + self.lows_wide + self.closes_wide) / 3.0
        return self._vwaps

    @property
    def universe(self) -> List[str]:
        return sorted(self._long["ticker"].unique().tolist())

    @property
    def date_range(self) -> tuple:
        return (self._long["timestamp"].min(), self._long["timestamp"].max())

    # ------------------------------------------------------------------
    # Slicing
    # ------------------------------------------------------------------
    def restrict(
        self,
        universe: Optional[List[str]] = None,
        start: Optional[str | pd.Timestamp] = None,
        end: Optional[str | pd.Timestamp] = None,
    ) -> "DataPanel":
        df = self._long
        if universe:
            df = df[df["ticker"].isin(universe)]
        if start:
            df = df[df["timestamp"] >= pd.Timestamp(start)]
        if end:
            df = df[df["timestamp"] <= pd.Timestamp(end)]
        return DataPanel(df.reset_index(drop=True))

    # ------------------------------------------------------------------
    # Forward-return helper (used by evaluator)
    # ------------------------------------------------------------------
    def forward_returns(self, horizon: int = 1, kind: str = "log") -> pd.DataFrame:
        """Return forward returns over `horizon` days, aligned to t.

        Convention: row at date t gives the return REALIZED between t and t+horizon.
        """
        closes = self.closes_wide
        if kind == "log":
            return np.log(closes.shift(-horizon) / closes)
        return closes.shift(-horizon) / closes - 1.0

    def __repr__(self) -> str:
        d_start, d_end = self.date_range
        return (
            f"<DataPanel {len(self.universe)} tickers × "
            f"{self.closes_wide.shape[0]} dates "
            f"({d_start.date()} → {d_end.date()})>"
        )
