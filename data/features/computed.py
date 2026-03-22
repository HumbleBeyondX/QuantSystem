"""Feature computation engine: ties FeatureStore with data sources.

Supports both batch mode (for backtesting) and streaming mode (for live).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pandas as pd

from core.logging import get_logger
from data.features.feature_store import FeatureStore
from data.features.fundamental import register_fundamental_features
from data.features.microstructure import register_microstructure_features
from data.features.sentiment import register_sentiment_features
from data.features.technical import register_technical_features

logger = get_logger(__name__)


def create_default_feature_store() -> FeatureStore:
    """Create a FeatureStore with all default features registered."""
    store = FeatureStore()
    register_technical_features(store)
    register_microstructure_features(store)
    register_fundamental_features(store)
    register_sentiment_features(store)
    logger.info("feature_store_initialized", total=len(store.available_features))
    return store


class StreamingFeatureEngine:
    """Computes features in streaming mode using a rolling buffer.

    Maintains a fixed-size DataFrame buffer per symbol and recomputes
    features on each new bar/tick arrival.
    """

    def __init__(
        self,
        store: Optional[FeatureStore] = None,
        buffer_size: int = 500,
        features: Optional[List[str]] = None,
    ) -> None:
        self._store = store or create_default_feature_store()
        self._buffer_size = buffer_size
        self._features = features or self._store.available_features
        self._buffers: Dict[str, pd.DataFrame] = {}

    def update(self, symbol: str, bar: Dict[str, Any]) -> Optional[Dict[str, float]]:
        """Update buffer with new bar and compute latest feature values.

        Args:
            symbol: Symbol identifier
            bar: Dict with keys: timestamp, open, high, low, close, volume

        Returns:
            Dict of feature_name -> latest value, or None if buffer too small.
        """
        # Initialize buffer
        if symbol not in self._buffers:
            self._buffers[symbol] = pd.DataFrame()

        # Append new bar
        new_row = pd.DataFrame([bar])
        self._buffers[symbol] = pd.concat(
            [self._buffers[symbol], new_row], ignore_index=True
        ).tail(self._buffer_size)

        buf = self._buffers[symbol]

        # Check minimum lookback
        min_lookback = self._store.get_min_lookback(self._features)
        if len(buf) < min_lookback:
            return None

        # Compute features
        try:
            result_df = self._store.compute(buf, self._features)
            # Return latest row as dict
            latest = result_df.iloc[-1]
            return {
                col: float(latest[col])
                for col in result_df.columns
                if col not in ("timestamp", "open", "high", "low", "close", "volume")
                and pd.notna(latest.get(col))
            }
        except Exception as e:
            logger.error("streaming_feature_error", symbol=symbol, error=str(e))
            return None

    def get_buffer(self, symbol: str) -> Optional[pd.DataFrame]:
        """Get the current buffer for a symbol."""
        return self._buffers.get(symbol)

    def reset(self, symbol: Optional[str] = None) -> None:
        """Reset buffer(s)."""
        if symbol:
            self._buffers.pop(symbol, None)
        else:
            self._buffers.clear()


class BatchFeatureEngine:
    """Computes features in batch mode for backtesting."""

    def __init__(
        self,
        store: Optional[FeatureStore] = None,
        features: Optional[List[str]] = None,
    ) -> None:
        self._store = store or create_default_feature_store()
        self._features = features

    def compute(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute all features on a full DataFrame."""
        return self._store.compute(df, self._features)

    def compute_multi_symbol(
        self, data: Dict[str, pd.DataFrame]
    ) -> Dict[str, pd.DataFrame]:
        """Compute features for multiple symbols."""
        results = {}
        for symbol, df in data.items():
            try:
                results[symbol] = self._store.compute(df, self._features)
            except Exception as e:
                logger.error("batch_feature_error", symbol=symbol, error=str(e))
                results[symbol] = df
        return results
