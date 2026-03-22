"""Technical analysis indicators.

Wraps TA-Lib (with pandas_ta fallback) for standard technical indicators.
All functions accept a DataFrame with OHLCV columns and return Series/DataFrame.
"""

from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd

# Try TA-Lib first, fall back to pandas_ta, then pure pandas
try:
    import talib

    HAS_TALIB = True
except ImportError:
    HAS_TALIB = False

try:
    import pandas_ta as ta

    HAS_PANDAS_TA = True
except ImportError:
    HAS_PANDAS_TA = False


# ============================================================
# Moving Averages
# ============================================================


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    if HAS_TALIB:
        return pd.Series(talib.EMA(series.values, timeperiod=period), index=series.index)
    return series.ewm(span=period, adjust=False).mean()


def compute_sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    if HAS_TALIB:
        return pd.Series(talib.SMA(series.values, timeperiod=period), index=series.index)
    return series.rolling(window=period).mean()


def compute_wma(series: pd.Series, period: int) -> pd.Series:
    """Weighted Moving Average."""
    if HAS_TALIB:
        return pd.Series(talib.WMA(series.values, timeperiod=period), index=series.index)
    weights = np.arange(1, period + 1, dtype=float)
    return series.rolling(window=period).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)


# ============================================================
# Oscillators
# ============================================================


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index."""
    if HAS_TALIB:
        return pd.Series(talib.RSI(series.values, timeperiod=period), index=series.index)
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(window=period).mean()
    # When loss is zero (pure uptrend), RSI = 100; when gain is zero, RSI = 0
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    # Handle edge cases: all gains → RSI = 100, all losses → RSI = 0
    rsi = rsi.where(loss != 0, 100.0)
    rsi = rsi.where(gain != 0, 0.0)
    return rsi


def compute_stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> pd.DataFrame:
    """Stochastic Oscillator (%K and %D)."""
    if HAS_TALIB:
        k, d = talib.STOCH(
            df["high"].values, df["low"].values, df["close"].values,
            fastk_period=k_period, slowk_period=d_period, slowd_period=d_period,
        )
        return pd.DataFrame({"stoch_k": k, "stoch_d": d}, index=df.index)
    low_min = df["low"].rolling(window=k_period).min()
    high_max = df["high"].rolling(window=k_period).max()
    k = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    d = k.rolling(window=d_period).mean()
    return pd.DataFrame({"stoch_k": k, "stoch_d": d}, index=df.index)


# ============================================================
# Trend Indicators
# ============================================================


def compute_macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """MACD (Moving Average Convergence Divergence)."""
    if HAS_TALIB:
        macd, signal_line, hist = talib.MACD(
            series.values, fastperiod=fast, slowperiod=slow, signalperiod=signal
        )
        return pd.DataFrame(
            {"macd": macd, "macd_signal": signal_line, "macd_hist": hist},
            index=series.index,
        )
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    hist = macd - signal_line
    return pd.DataFrame(
        {"macd": macd, "macd_signal": signal_line, "macd_hist": hist},
        index=series.index,
    )


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average Directional Index."""
    if HAS_TALIB:
        return pd.Series(
            talib.ADX(df["high"].values, df["low"].values, df["close"].values, timeperiod=period),
            index=df.index,
        )
    # Simplified ADX using Wilder smoothing
    high, low, close = df["high"], df["low"], df["close"]
    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    tr = compute_true_range(df)
    atr = tr.rolling(window=period).mean()
    plus_di = 100 * (plus_dm.rolling(window=period).mean() / atr.replace(0, np.nan))
    minus_di = 100 * (minus_dm.rolling(window=period).mean() / atr.replace(0, np.nan))
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di).replace(0, np.nan)
    return dx.rolling(window=period).mean()


# ============================================================
# Volatility Indicators
# ============================================================


def compute_bollinger(
    series: pd.Series, period: int = 20, std_dev: float = 2.0
) -> pd.DataFrame:
    """Bollinger Bands."""
    if HAS_TALIB:
        upper, middle, lower = talib.BBANDS(
            series.values, timeperiod=period, nbdevup=std_dev, nbdevdn=std_dev
        )
        return pd.DataFrame(
            {"bb_upper": upper, "bb_middle": middle, "bb_lower": lower},
            index=series.index,
        )
    middle = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    return pd.DataFrame(
        {
            "bb_upper": middle + std_dev * std,
            "bb_middle": middle,
            "bb_lower": middle - std_dev * std,
        },
        index=series.index,
    )


def compute_true_range(df: pd.DataFrame) -> pd.Series:
    """True Range."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range."""
    if HAS_TALIB:
        return pd.Series(
            talib.ATR(df["high"].values, df["low"].values, df["close"].values, timeperiod=period),
            index=df.index,
        )
    tr = compute_true_range(df)
    return tr.ewm(span=period, adjust=False).mean()


def compute_natr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Normalized ATR (ATR as percentage of close)."""
    atr = compute_atr(df, period)
    return (atr / df["close"]) * 100


# ============================================================
# Volume Indicators
# ============================================================


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """Volume Weighted Average Price (intraday reset assumed per session)."""
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cumulative_tpv = (typical_price * df["volume"]).cumsum()
    cumulative_vol = df["volume"].cumsum()
    return cumulative_tpv / cumulative_vol.replace(0, np.nan)


def compute_obv(df: pd.DataFrame) -> pd.Series:
    """On Balance Volume."""
    if HAS_TALIB:
        return pd.Series(
            talib.OBV(df["close"].values, df["volume"].values), index=df.index
        )
    direction = np.sign(df["close"].diff())
    return (direction * df["volume"]).cumsum()


def compute_volume_sma(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Volume Simple Moving Average."""
    return df["volume"].rolling(window=period).mean()


def compute_volume_ratio(df: pd.DataFrame, period: int = 20) -> pd.Series:
    """Current volume / average volume ratio."""
    avg_vol = df["volume"].rolling(window=period).mean()
    return df["volume"] / avg_vol.replace(0, np.nan)


# ============================================================
# Feature Registry
# ============================================================

# Map of feature_name -> (function, category, lookback)
TECHNICAL_FEATURES: Dict[str, tuple] = {
    # Moving averages
    "ema_9": (lambda df: compute_ema(df["close"], 9), "trend", 9),
    "ema_21": (lambda df: compute_ema(df["close"], 21), "trend", 21),
    "ema_55": (lambda df: compute_ema(df["close"], 55), "trend", 55),
    "ema_200": (lambda df: compute_ema(df["close"], 200), "trend", 200),
    "sma_20": (lambda df: compute_sma(df["close"], 20), "trend", 20),
    "sma_50": (lambda df: compute_sma(df["close"], 50), "trend", 50),
    # Oscillators
    "rsi_14": (lambda df: compute_rsi(df["close"], 14), "oscillator", 14),
    "rsi_7": (lambda df: compute_rsi(df["close"], 7), "oscillator", 7),
    "stochastic": (lambda df: compute_stochastic(df), "oscillator", 14),
    # Trend
    "macd": (lambda df: compute_macd(df["close"]), "trend", 26),
    "adx_14": (lambda df: compute_adx(df, 14), "trend", 28),
    # Volatility
    "bollinger_20": (lambda df: compute_bollinger(df["close"], 20), "volatility", 20),
    "atr_14": (lambda df: compute_atr(df, 14), "volatility", 14),
    "natr_14": (lambda df: compute_natr(df, 14), "volatility", 14),
    # Volume
    "vwap": (lambda df: compute_vwap(df), "volume", 1),
    "obv": (lambda df: compute_obv(df), "volume", 1),
    "volume_sma_20": (lambda df: compute_volume_sma(df, 20), "volume", 20),
    "volume_ratio": (lambda df: compute_volume_ratio(df, 20), "volume", 20),
}


def register_technical_features(store) -> None:
    """Register all technical features with a FeatureStore instance."""
    for name, (func, category, lookback) in TECHNICAL_FEATURES.items():
        store.register(
            name=name,
            func=func,
            category=f"technical_{category}",
            lookback=lookback,
        )
