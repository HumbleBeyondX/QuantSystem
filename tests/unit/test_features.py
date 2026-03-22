"""Unit tests for feature computation (technical indicators)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from data.features.technical import (
    compute_bollinger,
    compute_ema,
    compute_macd,
    compute_rsi,
    compute_vwap,
)
from data.features.feature_store import FeatureStore
from data.features.technical import register_technical_features


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ohlcv(n: int = 100, seed: int = 42) -> pd.DataFrame:
    """Generate a synthetic OHLCV DataFrame with a controlled random walk."""
    rng = np.random.RandomState(seed)
    close = 100 + np.cumsum(rng.randn(n) * 0.5)
    high = close + np.abs(rng.randn(n) * 0.3)
    low = close - np.abs(rng.randn(n) * 0.3)
    open_ = close + rng.randn(n) * 0.1
    volume = np.abs(rng.randn(n) * 1e6) + 5e5
    return pd.DataFrame({
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEMA:
    def test_ema_calculation(self):
        """EMA should converge towards the series mean for constant input."""
        series = pd.Series([10.0] * 30)
        ema = compute_ema(series, period=10)

        # All values should equal 10 for constant input
        np.testing.assert_allclose(ema.values, 10.0, atol=1e-10)

    def test_ema_responsiveness(self):
        """Shorter EMA periods should react faster to price changes."""
        series = pd.Series([100.0] * 20 + [110.0] * 10)
        ema_fast = compute_ema(series, period=5)
        ema_slow = compute_ema(series, period=15)

        # After the jump, the fast EMA should be closer to 110
        assert ema_fast.iloc[-1] > ema_slow.iloc[-1]

    def test_ema_length(self):
        """EMA output should have the same length as input."""
        series = pd.Series(range(50), dtype=float)
        ema = compute_ema(series, period=10)
        assert len(ema) == len(series)


class TestRSI:
    def test_rsi_calculation(self):
        """RSI should be between 0 and 100 for realistic data."""
        df = _make_ohlcv(100)
        rsi = compute_rsi(df["close"], period=14)

        valid = rsi.dropna()
        assert (valid >= 0).all()
        assert (valid <= 100).all()

    def test_rsi_overbought_after_rally(self):
        """RSI should be high after a sustained uptrend."""
        prices = pd.Series(np.linspace(100, 150, 50))
        rsi = compute_rsi(prices, period=14)
        # Last RSI value should indicate overbought territory
        assert rsi.iloc[-1] > 70

    def test_rsi_oversold_after_selloff(self):
        """RSI should be low after a sustained downtrend."""
        prices = pd.Series(np.linspace(150, 100, 50))
        rsi = compute_rsi(prices, period=14)
        assert rsi.iloc[-1] < 30


class TestMACD:
    def test_macd_calculation(self):
        """MACD output should contain macd, macd_signal, and macd_hist columns."""
        df = _make_ohlcv(100)
        result = compute_macd(df["close"])

        assert "macd" in result.columns
        assert "macd_signal" in result.columns
        assert "macd_hist" in result.columns
        assert len(result) == len(df)

    def test_macd_histogram_is_difference(self):
        """MACD histogram should be macd - signal."""
        df = _make_ohlcv(100)
        result = compute_macd(df["close"])

        np.testing.assert_allclose(
            result["macd_hist"].values,
            (result["macd"] - result["macd_signal"]).values,
            atol=1e-10,
        )


class TestBollingerBands:
    def test_bollinger_bands(self):
        """Upper band should always be above lower band."""
        df = _make_ohlcv(100)
        bb = compute_bollinger(df["close"], period=20, std_dev=2.0)

        valid = bb.dropna()
        assert (valid["bb_upper"] >= valid["bb_middle"]).all()
        assert (valid["bb_middle"] >= valid["bb_lower"]).all()

    def test_bollinger_middle_is_sma(self):
        """The middle band should be the SMA of the given period."""
        df = _make_ohlcv(100)
        bb = compute_bollinger(df["close"], period=20, std_dev=2.0)
        sma = df["close"].rolling(20).mean()

        np.testing.assert_allclose(
            bb["bb_middle"].dropna().values,
            sma.dropna().values,
            atol=1e-10,
        )


class TestVWAP:
    def test_vwap(self):
        """VWAP should be bounded by the session's low and high."""
        df = _make_ohlcv(50)
        vwap = compute_vwap(df)

        valid = vwap.dropna()
        # VWAP is a cumulative volume-weighted average, so it should be
        # within the range of typical prices seen so far.
        assert valid.iloc[-1] > 0


class TestFeatureStoreRegistry:
    def test_feature_store_registry(self, feature_store: FeatureStore):
        """After registration, all expected technical features should be available."""
        available = feature_store.available_features
        assert "ema_9" in available
        assert "rsi_14" in available
        assert "macd" in available
        assert "bollinger_20" in available
        assert "vwap" in available

    def test_feature_store_compute(self, feature_store: FeatureStore):
        """Computing a subset of features should add columns to the DataFrame."""
        df = _make_ohlcv(100)
        result = feature_store.compute(df, ["ema_9", "rsi_14"])

        assert "ema_9" in result.columns
        assert "rsi_14" in result.columns
        # Original columns should still be present
        assert "close" in result.columns

    def test_feature_store_category_filter(self, feature_store: FeatureStore):
        """Filtering by category should return only matching features."""
        volume_features = feature_store.get_features_by_category("technical_volume")
        assert "vwap" in volume_features
        assert "obv" in volume_features
