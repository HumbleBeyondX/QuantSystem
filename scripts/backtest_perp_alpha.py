#!/usr/bin/env python3
"""Crypto Perpetual Alpha Machine (CPAM) Strategy Backtest.

Three-pillar signal architecture for BTC perpetual futures:

  Pillar 1 — Macro Liquidity (35%):
    Cross-asset signals (DXY, SPX, Gold, VIX) fetched via yfinance.
    - DXY inverse momentum: dollar weakness → crypto bid
    - BTC-SPX rolling correlation regime shift
    - VIX risk appetite: low VIX → risk-on
    - Cross-asset momentum factor (multi-asset trend)

  Pillar 2 — ML Alpha (40%):
    LightGBM classifier with walk-forward retraining.
    - 50+ engineered features (microstructure, momentum, volatility, seasonality)
    - Target: sign(forward N-bar return)
    - Retrain every 180 bars (30 days)
    - Probability output as signal strength

  Pillar 3 — Time Series Stats (25%):
    - Rolling Hurst exponent (trending vs mean-reverting regime)
    - GARCH(1,1) conditional volatility forecast
    - Autocorrelation momentum signal
    - Variance ratio test for random walk

  Risk: Relaxed for medium-frequency, 10x leverage.

Usage:
    python scripts/backtest_perp_alpha.py
    python scripts/backtest_perp_alpha.py --sweep
    python scripts/backtest_perp_alpha.py --start 2022-01-01 --leverage 10
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy import stats as sp_stats

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ────────────────────────────────────────────────────────────
#  Optional heavy imports
# ────────────────────────────────────────────────────────────
try:
    import lightgbm as lgb
    HAS_LGB = True
except ImportError:
    HAS_LGB = False

try:
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

try:
    from arch import arch_model
    HAS_ARCH = True
except ImportError:
    HAS_ARCH = False


# ════════════════════════════════════════════════════════════
#  PILLAR 1: MACRO LIQUIDITY DATA & SIGNALS
# ════════════════════════════════════════════════════════════

def fetch_macro_data(start: str = "2019-06-01", end: str = "2026-12-31") -> pd.DataFrame:
    """Fetch macro asset 4H-aligned daily data via yfinance.

    Assets: DXY (UUP ETF proxy), SPX (SPY), Gold (GLD), VIX (^VIX)
    Returns daily data that will be forward-filled to 4H bars.
    """
    import yfinance as yf

    tickers = {
        "dxy": "UUP",    # Dollar ETF as DXY proxy
        "spx": "SPY",    # S&P 500
        "gold": "GLD",   # Gold ETF
        "vix": "^VIX",   # VIX
    }

    frames = {}
    for name, ticker in tickers.items():
        try:
            data = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
            if len(data) > 0:
                # Handle yfinance multi-level columns
                if isinstance(data.columns, pd.MultiIndex):
                    close_col = data["Close"]
                    if isinstance(close_col, pd.DataFrame):
                        close_col = close_col.iloc[:, 0]
                else:
                    close_col = data["Close"]
                frames[name] = close_col.rename(name)
        except Exception as e:
            print(f"  [WARN] Failed to fetch {name} ({ticker}): {e}")

    if not frames:
        return pd.DataFrame()

    macro = pd.concat(frames.values(), axis=1)
    macro.index = pd.to_datetime(macro.index).tz_localize(None)
    macro = macro.sort_index()
    macro = macro.ffill().bfill()
    return macro


def compute_macro_features(btc_df: pd.DataFrame, macro_df: pd.DataFrame) -> pd.DataFrame:
    """Merge macro data into BTC DataFrame and compute liquidity signals."""
    df = btc_df.copy()

    # Align macro (daily) to BTC (4H) via forward-fill
    df["date"] = df["timestamp"].dt.tz_localize(None).dt.normalize()

    if not macro_df.empty:
        macro_daily = macro_df.copy()
        macro_daily.index = pd.to_datetime(macro_daily.index).tz_localize(None).normalize()
        # Remove duplicate dates if any
        macro_daily = macro_daily[~macro_daily.index.duplicated(keep="last")]
        df = df.merge(macro_daily, left_on="date", right_index=True, how="left")
        df = df.ffill()

        # ── DXY inverse momentum (20-period) ──
        if "dxy" in df.columns:
            dxy_ret = df["dxy"].pct_change(20)
            df["dxy_inv_mom"] = -dxy_ret  # Negative = dollar strength → bearish crypto
            df["dxy_zscore"] = (df["dxy"] - df["dxy"].rolling(60).mean()) / df["dxy"].rolling(60).std().replace(0, 1)
        else:
            df["dxy_inv_mom"] = 0
            df["dxy_zscore"] = 0

        # ── BTC-SPX rolling correlation ──
        if "spx" in df.columns:
            btc_ret = df["close"].pct_change()
            spx_ret = df["spx"].pct_change()
            df["btc_spx_corr"] = btc_ret.rolling(60).corr(spx_ret)
            df["spx_mom20"] = df["spx"].pct_change(20)
        else:
            df["btc_spx_corr"] = 0
            df["spx_mom20"] = 0

        # ── VIX regime ──
        if "vix" in df.columns:
            df["vix_zscore"] = (df["vix"] - df["vix"].rolling(60).mean()) / df["vix"].rolling(60).std().replace(0, 1)
            df["vix_regime"] = np.where(df["vix"] > 25, -1,  # High VIX = risk off
                                np.where(df["vix"] < 15, 1, 0))  # Low VIX = risk on
        else:
            df["vix_zscore"] = 0
            df["vix_regime"] = 0

        # ── Gold momentum (safe haven flow) ──
        if "gold" in df.columns:
            df["gold_mom20"] = df["gold"].pct_change(20)
        else:
            df["gold_mom20"] = 0
    else:
        for col in ["dxy_inv_mom", "dxy_zscore", "btc_spx_corr", "spx_mom20",
                     "vix_zscore", "vix_regime", "gold_mom20"]:
            df[col] = 0

    # ── Macro composite signal ──
    # Positive = bullish environment for crypto
    df["macro_signal"] = (
        0.30 * df["dxy_inv_mom"].clip(-0.1, 0.1) * 10  # DXY inverse
        + 0.25 * df["vix_regime"]                        # VIX risk appetite
        + 0.20 * df["spx_mom20"].clip(-0.2, 0.2) * 5   # SPX momentum (risk-on)
        - 0.15 * df["gold_mom20"].clip(-0.2, 0.2) * 5   # Gold inverse (risk-off hedge)
        + 0.10 * df["btc_spx_corr"].clip(-1, 1)         # Correlation regime
    )

    df.drop(columns=["date"], errors="ignore", inplace=True)
    return df


# ════════════════════════════════════════════════════════════
#  PILLAR 2: ML FEATURE ENGINEERING & MODEL
# ════════════════════════════════════════════════════════════

def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """Engineer 50+ features for ML model."""
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]

    # ── Returns at multiple horizons ──
    for p in [1, 2, 3, 6, 12, 24]:
        df[f"ret_{p}"] = c.pct_change(p)

    # ── Volatility features ──
    log_ret = np.log(c / c.shift(1))
    df["realized_vol_6"] = log_ret.rolling(6).std() * np.sqrt(6 * 365)
    df["realized_vol_24"] = log_ret.rolling(24).std() * np.sqrt(6 * 365)
    df["realized_vol_72"] = log_ret.rolling(72).std() * np.sqrt(6 * 365)
    df["vol_ratio_6_24"] = df["realized_vol_6"] / df["realized_vol_24"].replace(0, np.nan)
    df["vol_ratio_24_72"] = df["realized_vol_24"] / df["realized_vol_72"].replace(0, np.nan)
    df["garman_klass"] = (0.5 * np.log(h / l) ** 2
                          - (2 * np.log(2) - 1) * np.log(c / df["open"]) ** 2)
    df["gk_vol_20"] = df["garman_klass"].rolling(20).mean()

    # ── Microstructure / Liquidity features ──
    df["amihud"] = (log_ret.abs() / v.replace(0, np.nan)).rolling(20).mean()
    df["amihud_zscore"] = (df["amihud"] - df["amihud"].rolling(60).mean()) / df["amihud"].rolling(60).std().replace(0, 1)
    df["kyle_lambda"] = (c.diff().abs() / v.replace(0, np.nan)).rolling(20).mean()
    df["vol_price_corr"] = v.rolling(20).corr(c.diff().abs())
    df["bar_range_pct"] = (h - l) / c
    df["bar_range_vol_ratio"] = df["bar_range_pct"] / (v / v.rolling(20).mean()).replace(0, np.nan)

    # ── Momentum features ──
    for p in [6, 12, 24, 48]:
        df[f"mom_{p}"] = c / c.shift(p) - 1

    # EMA distances
    ema8 = c.ewm(span=8, adjust=False).mean()
    ema21 = c.ewm(span=21, adjust=False).mean()
    ema55 = c.ewm(span=55, adjust=False).mean()
    df["ema8_dist"] = (c - ema8) / ema8
    df["ema21_dist"] = (c - ema21) / ema21
    df["ema55_dist"] = (c - ema55) / ema55
    df["ema_spread_8_21"] = (ema8 - ema21) / ema21

    # RSI
    delta = c.diff()
    gain = delta.where(delta > 0, 0.0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = (100 - 100 / (1 + rs)).fillna(50)

    # MACD
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    macd_sig = macd.ewm(span=9, adjust=False).mean()
    df["macd_hist"] = macd - macd_sig
    df["macd_hist_norm"] = df["macd_hist"] / c  # Normalize by price

    # ADX
    plus_dm = h.diff().clip(lower=0)
    minus_dm = (-l.diff()).clip(lower=0)
    plus_dm = plus_dm.where(plus_dm > minus_dm, 0)
    minus_dm = minus_dm.where(minus_dm > plus_dm, 0)
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    atr14 = tr.ewm(span=14, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(span=14, adjust=False).mean() / atr14.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(span=14, adjust=False).mean() / atr14.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["adx"] = dx.ewm(span=14, adjust=False).mean()
    df["di_diff"] = plus_di - minus_di

    # ── Volume features ──
    df["vol_zscore"] = (v - v.rolling(60).mean()) / v.rolling(60).std().replace(0, 1)
    df["vol_trend"] = v.rolling(6).mean() / v.rolling(24).mean().replace(0, np.nan)
    df["obv_slope"] = (v * np.sign(c.diff())).cumsum()
    obv = df["obv_slope"]
    df["obv_slope_20"] = (obv - obv.shift(20)) / obv.shift(20).abs().replace(0, np.nan)

    # ── ATR & Bollinger ──
    df["atr"] = atr14
    df["natr"] = atr14 / c
    sma20 = c.rolling(20).mean()
    std20 = c.rolling(20).std()
    df["bb_pct"] = (c - (sma20 - 2 * std20)) / (4 * std20).replace(0, np.nan)
    df["bb_width"] = (4 * std20) / sma20.replace(0, np.nan)

    # ── Seasonality features ──
    if df["timestamp"].dtype != 'datetime64[ns]':
        ts = pd.to_datetime(df["timestamp"])
    else:
        ts = df["timestamp"]
    df["hour_sin"] = np.sin(2 * np.pi * ts.dt.hour / 24)
    df["hour_cos"] = np.cos(2 * np.pi * ts.dt.hour / 24)
    df["dow_sin"] = np.sin(2 * np.pi * ts.dt.dayofweek / 7)
    df["dow_cos"] = np.cos(2 * np.pi * ts.dt.dayofweek / 7)
    df["month_sin"] = np.sin(2 * np.pi * ts.dt.month / 12)

    # ── Higher-order statistics ──
    df["skew_20"] = log_ret.rolling(20).skew()
    df["kurt_20"] = log_ret.rolling(20).kurt()

    return df


# Feature columns for ML model
ML_FEATURE_COLS = [
    # Returns
    "ret_1", "ret_2", "ret_3", "ret_6", "ret_12", "ret_24",
    # Volatility
    "realized_vol_6", "realized_vol_24", "realized_vol_72",
    "vol_ratio_6_24", "vol_ratio_24_72", "gk_vol_20",
    # Microstructure
    "amihud_zscore", "kyle_lambda", "vol_price_corr",
    "bar_range_pct", "bar_range_vol_ratio",
    # Momentum
    "mom_6", "mom_12", "mom_24", "mom_48",
    "ema8_dist", "ema21_dist", "ema55_dist", "ema_spread_8_21",
    "rsi", "macd_hist_norm", "adx", "di_diff",
    # Volume
    "vol_zscore", "vol_trend", "obv_slope_20",
    # Bollinger / ATR
    "natr", "bb_pct", "bb_width",
    # Seasonality
    "hour_sin", "hour_cos", "dow_sin", "dow_cos", "month_sin",
    # Higher-order
    "skew_20", "kurt_20",
    # Macro (if available)
    "dxy_inv_mom", "dxy_zscore", "btc_spx_corr", "spx_mom20",
    "vix_zscore", "vix_regime", "gold_mom20", "macro_signal",
]


class WalkForwardML:
    """Walk-forward ML model with periodic retraining."""

    def __init__(
        self,
        train_window: int = 500,
        retrain_every: int = 180,
        forward_bars: int = 6,
        min_train_samples: int = 300,
    ):
        self.train_window = train_window
        self.retrain_every = retrain_every
        self.forward_bars = forward_bars
        self.min_train_samples = min_train_samples
        self.model = None
        self.scaler = StandardScaler() if HAS_SKLEARN else None
        self._bars_since_train = retrain_every + 1  # Force initial training
        self._feature_importance: Dict[str, float] = {}

    def _get_features(self, df: pd.DataFrame, idx: int, n: int) -> Tuple[np.ndarray, list]:
        """Extract feature matrix from df[idx-n:idx]."""
        cols = [c for c in ML_FEATURE_COLS if c in df.columns]
        chunk = df.iloc[max(0, idx - n):idx][cols]
        X = chunk.values
        return X, cols

    def _get_target(self, df: pd.DataFrame, idx: int, n: int) -> np.ndarray:
        """Binary target: 1 if forward return > 0, else 0."""
        chunk = df.iloc[max(0, idx - n):idx]
        fwd = df["close"].shift(-self.forward_bars)
        fwd_ret = (fwd / df["close"] - 1).iloc[max(0, idx - n):idx]
        return (fwd_ret > 0).astype(int).values

    def predict(self, df: pd.DataFrame, idx: int) -> Tuple[float, float]:
        """Return (direction_score [-1,1], confidence [0,1])."""
        if self.model is None:
            return 0.0, 0.0

        cols = [c for c in ML_FEATURE_COLS if c in df.columns]
        row = df.iloc[idx][cols].values.reshape(1, -1)

        # Handle NaN (cast to float first for mixed-type safety)
        row = row.astype(np.float64)
        if np.any(np.isnan(row)):
            row = np.nan_to_num(row, nan=0.0)

        if self.scaler is not None:
            row = self.scaler.transform(row)

        if HAS_LGB:
            prob = self.model.predict(row)[0]
        else:
            prob = self.model.predict_proba(row)[0][1]

        # Convert probability to direction + confidence
        direction = 2 * prob - 1  # Map [0,1] → [-1,1]
        confidence = abs(direction)
        return direction, confidence

    def maybe_retrain(self, df: pd.DataFrame, idx: int) -> bool:
        """Retrain if enough bars have passed."""
        self._bars_since_train += 1
        if self._bars_since_train < self.retrain_every:
            return False
        if idx < self.min_train_samples + self.forward_bars:
            return False

        n = min(self.train_window, idx - self.forward_bars)
        if n < self.min_train_samples:
            return False

        X, cols = self._get_features(df, idx - self.forward_bars, n)
        y = self._get_target(df, idx - self.forward_bars, n)

        # Remove rows with NaN
        mask = ~(np.isnan(X).any(axis=1) | np.isnan(y))
        X, y = X[mask], y[mask]

        if len(X) < self.min_train_samples // 2:
            return False

        if self.scaler is not None:
            X = self.scaler.fit_transform(X)

        try:
            if HAS_LGB:
                train_data = lgb.Dataset(X, label=y)
                params = {
                    "objective": "binary",
                    "metric": "binary_logloss",
                    "num_leaves": 31,
                    "learning_rate": 0.05,
                    "feature_fraction": 0.8,
                    "bagging_fraction": 0.8,
                    "bagging_freq": 5,
                    "verbose": -1,
                    "min_data_in_leaf": 20,
                    "lambda_l1": 0.1,
                    "lambda_l2": 0.1,
                }
                self.model = lgb.train(params, train_data, num_boost_round=100)
                imp = dict(zip(cols, self.model.feature_importance("gain")))
                self._feature_importance = {k: round(v, 2) for k, v in
                                            sorted(imp.items(), key=lambda x: -x[1])[:10]}
            elif HAS_SKLEARN:
                self.model = GradientBoostingClassifier(
                    n_estimators=100, max_depth=4, learning_rate=0.05,
                    subsample=0.8, random_state=42,
                )
                self.model.fit(X, y)
                imp = dict(zip(cols, self.model.feature_importances_))
                self._feature_importance = {k: round(v, 4) for k, v in
                                            sorted(imp.items(), key=lambda x: -x[1])[:10]}
        except Exception as e:
            print(f"    [ML] Train error: {e}")
            return False

        self._bars_since_train = 0
        return True


# ════════════════════════════════════════════════════════════
#  PILLAR 3: TIME SERIES STATISTICAL SIGNALS
# ════════════════════════════════════════════════════════════

def rolling_hurst(series: pd.Series, window: int = 100) -> pd.Series:
    """Rolling Hurst exponent via R/S analysis.

    H > 0.5 → trending (persistent), H < 0.5 → mean-reverting (anti-persistent).
    """
    def _hurst_rs(x):
        if len(x) < 20:
            return 0.5
        ts = np.array(x)
        mean_ts = ts.mean()
        deviate = np.cumsum(ts - mean_ts)
        R = deviate.max() - deviate.min()
        S = ts.std(ddof=1)
        if S == 0 or R == 0:
            return 0.5
        return np.log(R / S) / np.log(len(ts))

    returns = series.pct_change().dropna()
    hurst = returns.rolling(window).apply(_hurst_rs, raw=True)
    return hurst.reindex(series.index)


def rolling_autocorr(series: pd.Series, lag: int = 6, window: int = 60) -> pd.Series:
    """Rolling autocorrelation at specified lag."""
    ret = series.pct_change()
    return ret.rolling(window).apply(lambda x: pd.Series(x).autocorr(lag=lag), raw=False)


def variance_ratio(series: pd.Series, period: int = 6, window: int = 120) -> pd.Series:
    """Rolling variance ratio test.

    VR > 1 → trending, VR < 1 → mean-reverting, VR ≈ 1 → random walk.
    """
    log_ret = np.log(series / series.shift(1))
    var_1 = log_ret.rolling(window).var()
    log_ret_k = np.log(series / series.shift(period))
    var_k = log_ret_k.rolling(window).var()
    return var_k / (period * var_1).replace(0, np.nan)


def garch_vol_forecast(series: pd.Series, window: int = 250) -> pd.Series:
    """Simplified GARCH(1,1) via EWMA approximation.

    Uses exponentially weighted variance with lambda=0.94 (RiskMetrics).
    """
    log_ret = np.log(series / series.shift(1))
    # EWMA variance (lambda=0.94 approximation of GARCH)
    ewma_var = log_ret.pow(2).ewm(span=int(1 / (1 - 0.94)), adjust=False).mean()
    garch_vol = np.sqrt(ewma_var * 6 * 365)  # Annualized for 4H bars
    return garch_vol


def compute_ts_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add time-series statistical features to DataFrame."""
    c = df["close"]

    # Hurst exponent
    df["hurst_100"] = rolling_hurst(c, 100)
    df["hurst_signal"] = df["hurst_100"] - 0.5  # >0 = trending, <0 = mean-reverting

    # Autocorrelation momentum
    df["autocorr_6"] = rolling_autocorr(c, lag=6, window=60)
    df["autocorr_1"] = rolling_autocorr(c, lag=1, window=60)

    # Variance ratio
    df["var_ratio_6"] = variance_ratio(c, period=6, window=120)
    df["vr_signal"] = df["var_ratio_6"] - 1  # >0 trending, <0 mean-reverting

    # GARCH volatility forecast
    df["garch_vol"] = garch_vol_forecast(c)
    realized = np.log(c / c.shift(1)).rolling(24).std() * np.sqrt(6 * 365)
    df["vol_premium"] = df["garch_vol"] - realized  # Positive = vol expanding

    # Composite time-series signal
    # Trending + positive autocorrelation = ride momentum
    # Mean-reverting + negative autocorrelation = fade moves
    df["ts_trend_score"] = (
        0.40 * df["hurst_signal"].clip(-0.3, 0.3) / 0.3
        + 0.30 * df["vr_signal"].clip(-1, 1)
        + 0.30 * df["autocorr_6"].clip(-0.3, 0.3) / 0.3
    )

    return df


# ════════════════════════════════════════════════════════════
#  BACKTEST ENGINE
# ════════════════════════════════════════════════════════════

class CryptoAlphaBacktest:
    """Three-pillar crypto perpetual futures backtest engine."""

    def __init__(
        self,
        initial_capital: float = 10_000,
        leverage: float = 10.0,
        # ── Signal weights ──
        macro_weight: float = 0.35,
        ml_weight: float = 0.40,
        ts_weight: float = 0.25,
        # ── Entry thresholds ──
        entry_threshold: float = 0.25,     # Composite signal threshold
        strong_entry: float = 0.45,        # Strong signal → bigger size
        # ── Position sizing ──
        base_position_pct: float = 0.10,
        max_position_pct: float = 0.25,
        # ── Risk (relaxed for medium-frequency) ──
        atr_stop_mult: float = 2.5,       # Wider stops
        atr_tp_mult: float = 5.0,         # Bigger targets
        trailing_stop_atr: float = 3.0,
        trailing_activate_atr: float = 2.0,
        max_holding_bars: int = 48,        # 8 days
        # ── Limits ──
        max_drawdown_pct: float = 0.30,    # 30% DD tolerance
        cooldown_bars: int = 3,
        max_trades_per_day: int = 3,
        cb_cooldown_bars: int = 48,
        # ── ML config ──
        ml_train_window: int = 500,
        ml_retrain_every: int = 180,
        ml_forward_bars: int = 6,
        ml_confidence_min: float = 0.15,   # Min ML confidence to trade
        # ── Fees ──
        fee_rate: float = 0.0005,
        # ── Regime adaptation ──
        use_regime_adapt: bool = True,     # Adapt stops by Hurst
    ):
        self.initial_capital = initial_capital
        self.leverage = leverage
        self.macro_weight = macro_weight
        self.ml_weight = ml_weight
        self.ts_weight = ts_weight
        self.entry_threshold = entry_threshold
        self.strong_entry = strong_entry
        self.base_position_pct = base_position_pct
        self.max_position_pct = max_position_pct
        self.atr_stop_mult = atr_stop_mult
        self.atr_tp_mult = atr_tp_mult
        self.trailing_stop_atr = trailing_stop_atr
        self.trailing_activate_atr = trailing_activate_atr
        self.max_holding_bars = max_holding_bars
        self.max_drawdown_pct = max_drawdown_pct
        self.cooldown_bars = cooldown_bars
        self.max_trades_per_day = max_trades_per_day
        self.cb_cooldown_bars = cb_cooldown_bars
        self.ml_train_window = ml_train_window
        self.ml_retrain_every = ml_retrain_every
        self.ml_forward_bars = ml_forward_bars
        self.ml_confidence_min = ml_confidence_min
        self.fee_rate = fee_rate
        self.use_regime_adapt = use_regime_adapt

        self._reset_state()

    def _reset_state(self):
        self._cash = self.initial_capital
        self._position = 0.0
        self._entry_price = 0.0
        self._entry_atr = 0.0
        self._stop_mult_used = 0.0
        self._tp_mult_used = 0.0
        self._highest_since_entry = 0.0
        self._lowest_since_entry = 0.0
        self._peak_equity = self.initial_capital
        self._bars_since_trade = 999
        self._bars_in_position = 0
        self._daily_trade_count = 0
        self._current_day = ""
        self._circuit_breaker = False
        self._cb_bars = 0

        self._equity_curve: List[Dict] = []
        self._trades: List[Dict] = []

    def _unrealized_pnl(self, price: float) -> float:
        if abs(self._position) < 1e-12:
            return 0.0
        return (price - self._entry_price) * self._position

    def _equity(self, price: float) -> float:
        return self._cash + self._unrealized_pnl(price)

    def _open_position(self, price, direction, strength, atr, stop_m, tp_m,
                       timestamp, signals):
        equity = self._equity(price)
        capped_equity = min(equity, self.initial_capital * 5)
        pos_pct = min(self.base_position_pct * strength, self.max_position_pct)
        notional = capped_equity * pos_pct * self.leverage
        qty = (notional / price) * direction
        cost = abs(qty * price * self.fee_rate)

        self._position = qty
        self._entry_price = price
        self._entry_atr = atr
        self._stop_mult_used = stop_m
        self._tp_mult_used = tp_m
        self._cash -= cost
        self._bars_in_position = 0
        self._highest_since_entry = price
        self._lowest_since_entry = price
        self._bars_since_trade = 0
        self._daily_trade_count += 1

        self._trades.append({
            "timestamp": timestamp,
            "action": "LONG" if direction > 0 else "SHORT",
            "price": round(price, 2),
            "quantity": round(abs(qty), 6),
            "notional": round(abs(qty * price), 2),
            "leverage_used": round(abs(qty * price) / max(equity, 1), 2),
            "fee": round(cost, 2),
            "strength": round(strength, 3),
            "stop_mult": round(stop_m, 2),
            "tp_mult": round(tp_m, 2),
            **{f"sig_{k}": round(v, 4) for k, v in signals.items()},
        })

    def _close_position(self, price, reason, timestamp):
        if abs(self._position) < 1e-12:
            return
        pnl = (price - self._entry_price) * self._position
        cost = abs(self._position * price * self.fee_rate)
        self._cash += pnl - cost

        direction = "LONG" if self._position > 0 else "SHORT"
        notional = abs(self._position * self._entry_price)
        pnl_pct = pnl / max(notional, 1) * 100

        self._trades.append({
            "timestamp": timestamp,
            "action": f"CLOSE_{direction}",
            "price": round(price, 2),
            "quantity": round(abs(self._position), 6),
            "pnl": round(pnl - cost, 2),
            "pnl_pct": round(pnl_pct, 2),
            "fee": round(cost, 2),
            "reason": reason,
            "entry_price": round(self._entry_price, 2),
            "bars_held": self._bars_in_position,
        })

        self._position = 0.0
        self._entry_price = 0.0
        self._entry_atr = 0.0
        self._bars_in_position = 0
        self._bars_since_trade = 0

    def run(self, df: pd.DataFrame, macro_df: pd.DataFrame = None) -> Dict[str, Any]:
        """Run the three-pillar backtest."""
        self._reset_state()

        print(f"\n{'=' * 64}")
        print(f"  CRYPTO PERPETUAL ALPHA MACHINE (CPAM) — BACKTEST")
        print(f"  Capital: ${self.initial_capital:,.0f} | Leverage: {self.leverage}x")
        print(f"  Bars: {len(df):,} | Weights: Macro={self.macro_weight:.0%} "
              f"ML={self.ml_weight:.0%} TS={self.ts_weight:.0%}")
        print(f"  Stop: {self.atr_stop_mult}x ATR | TP: {self.atr_tp_mult}x ATR | "
              f"Entry threshold: {self.entry_threshold}")
        print(f"{'=' * 64}")

        # Step 1: Compute macro features
        print("  [1/4] Computing macro liquidity features...")
        if macro_df is not None and not macro_df.empty:
            df = compute_macro_features(df, macro_df)
        else:
            for col in ["dxy_inv_mom", "dxy_zscore", "btc_spx_corr", "spx_mom20",
                         "vix_zscore", "vix_regime", "gold_mom20", "macro_signal"]:
                if col not in df.columns:
                    df[col] = 0

        # Step 2: Engineer ML features
        print("  [2/4] Engineering 50+ ML features...")
        df = engineer_features(df)

        # Step 3: Time series features
        print("  [3/4] Computing time-series statistical features...")
        df = compute_ts_features(df)

        # Step 4: Run backtest loop
        print("  [4/4] Running walk-forward backtest...")

        ml = WalkForwardML(
            train_window=self.ml_train_window,
            retrain_every=self.ml_retrain_every,
            forward_bars=self.ml_forward_bars,
        )

        warmup = 250  # Need enough for Hurst, EMAs, etc.
        timestamps = df["timestamp"].astype(str).values
        n = len(df)
        train_count = 0

        for idx in range(n):
            ts = timestamps[idx]
            row = df.iloc[idx]
            c = float(row["close"])
            h = float(row["high"])
            lo = float(row["low"])

            self._bars_since_trade += 1
            if abs(self._position) > 1e-12:
                self._bars_in_position += 1

            # Trailing high/low
            if self._position > 0:
                self._highest_since_entry = max(self._highest_since_entry, h)
            elif self._position < 0:
                self._lowest_since_entry = min(self._lowest_since_entry, lo)

            # Equity & DD
            equity = self._equity(c)
            if equity > self._peak_equity:
                self._peak_equity = equity
            if self._peak_equity > 0:
                dd = (self._peak_equity - equity) / self._peak_equity
                if self._circuit_breaker:
                    self._cb_bars += 1
                    if self._cb_bars >= self.cb_cooldown_bars and abs(self._position) < 1e-12:
                        self._circuit_breaker = False
                        self._peak_equity = equity
                elif dd > self.max_drawdown_pct:
                    self._circuit_breaker = True
                    self._cb_bars = 0

            day = ts[:10]
            if day != self._current_day:
                self._daily_trade_count = 0
                self._current_day = day

            if idx < warmup:
                self._equity_curve.append({
                    "timestamp": ts, "total_value": equity, "cash": self._cash, "n_positions": 0,
                })
                continue

            atr_val = float(row.get("atr", 0))
            if np.isnan(atr_val) or atr_val == 0:
                self._equity_curve.append({
                    "timestamp": ts, "total_value": equity, "cash": self._cash,
                    "n_positions": 1 if abs(self._position) > 1e-10 else 0,
                })
                continue

            # ── ML model retrain check ──
            if ml.maybe_retrain(df, idx):
                train_count += 1

            # ── Compute three-pillar signals ──
            macro_sig = float(row.get("macro_signal", 0))
            macro_sig = np.clip(macro_sig, -1, 1)

            ml_dir, ml_conf = ml.predict(df, idx)
            ml_sig = ml_dir  # [-1, 1]

            ts_trend = float(row.get("ts_trend_score", 0))
            ts_trend = np.clip(ts_trend, -1, 1)
            hurst_val = float(row.get("hurst_100", 0.5))

            # ── Composite signal ──
            composite = (
                self.macro_weight * macro_sig
                + self.ml_weight * ml_sig
                + self.ts_weight * ts_trend
            )

            # ── Regime-adaptive stops ──
            if self.use_regime_adapt and not np.isnan(hurst_val):
                if hurst_val > 0.55:  # Trending → wider stops, bigger targets
                    stop_m = self.atr_stop_mult * 1.2
                    tp_m = self.atr_tp_mult * 1.3
                elif hurst_val < 0.45:  # Mean-reverting → tighter stops, smaller targets
                    stop_m = self.atr_stop_mult * 0.8
                    tp_m = self.atr_tp_mult * 0.7
                else:
                    stop_m = self.atr_stop_mult
                    tp_m = self.atr_tp_mult
            else:
                stop_m = self.atr_stop_mult
                tp_m = self.atr_tp_mult

            # ════════════ EXIT ════════════
            if abs(self._position) > 1e-12:
                reason = None
                dsign = 1 if self._position > 0 else -1
                fixed_stop = self._entry_price - dsign * self._stop_mult_used * self._entry_atr
                tp = self._entry_price + dsign * self._tp_mult_used * self._entry_atr
                profit_dist = (c - self._entry_price) * dsign
                trailing_active = profit_dist > self.trailing_activate_atr * self._entry_atr

                if self._position > 0:
                    trail = self._highest_since_entry - self.trailing_stop_atr * self._entry_atr
                    if lo <= fixed_stop:
                        reason = "stop_loss"
                    elif trailing_active and lo <= trail:
                        reason = "trailing_stop"
                    elif h >= tp:
                        reason = "take_profit"
                    elif self._bars_in_position >= self.max_holding_bars:
                        reason = "max_holding"
                    elif self._circuit_breaker:
                        reason = "circuit_breaker"
                    # Signal reversal: composite flipped strongly negative
                    elif composite < -self.entry_threshold * 0.8:
                        reason = "signal_reversal"
                else:
                    trail = self._lowest_since_entry + self.trailing_stop_atr * self._entry_atr
                    if h >= fixed_stop:
                        reason = "stop_loss"
                    elif trailing_active and h >= trail:
                        reason = "trailing_stop"
                    elif lo <= tp:
                        reason = "take_profit"
                    elif self._bars_in_position >= self.max_holding_bars:
                        reason = "max_holding"
                    elif self._circuit_breaker:
                        reason = "circuit_breaker"
                    elif composite > self.entry_threshold * 0.8:
                        reason = "signal_reversal"

                if reason:
                    exit_p = c
                    if reason == "stop_loss":
                        exit_p = fixed_stop
                    elif reason == "take_profit":
                        exit_p = tp
                    elif reason == "trailing_stop":
                        exit_p = trail
                    self._close_position(exit_p, reason, ts)

            # ════════════ ENTRY ════════════
            if abs(self._position) < 1e-12:
                can_trade = (
                    self._bars_since_trade >= self.cooldown_bars
                    and self._daily_trade_count < self.max_trades_per_day
                    and not self._circuit_breaker
                    and ml_conf >= self.ml_confidence_min
                )

                if can_trade:
                    if composite > self.entry_threshold:
                        # LONG signal
                        excess = (composite - self.entry_threshold) / (1 - self.entry_threshold)
                        strength = min(0.5 + 0.5 * excess, 1.0)
                        if composite > self.strong_entry:
                            strength = min(strength + 0.15, 1.0)
                        sigs = {"macro": macro_sig, "ml": ml_sig, "ts": ts_trend,
                                "composite": composite, "hurst": hurst_val, "ml_conf": ml_conf}
                        self._open_position(c, 1, strength, atr_val, stop_m, tp_m, ts, sigs)

                    elif composite < -self.entry_threshold:
                        excess = (abs(composite) - self.entry_threshold) / (1 - self.entry_threshold)
                        strength = min(0.5 + 0.5 * excess, 1.0)
                        if composite < -self.strong_entry:
                            strength = min(strength + 0.15, 1.0)
                        sigs = {"macro": macro_sig, "ml": ml_sig, "ts": ts_trend,
                                "composite": composite, "hurst": hurst_val, "ml_conf": ml_conf}
                        self._open_position(c, -1, strength, atr_val, stop_m, tp_m, ts, sigs)

            equity = self._equity(c)
            self._equity_curve.append({
                "timestamp": ts, "total_value": equity, "cash": self._cash,
                "n_positions": 1 if abs(self._position) > 1e-10 else 0,
            })

        # Close at end
        if abs(self._position) > 1e-10:
            self._close_position(float(df["close"].iloc[-1]), "backtest_end", timestamps[-1])

        print(f"  ML retrained {train_count} times")
        if ml._feature_importance:
            print(f"  Top ML features: {list(ml._feature_importance.keys())[:5]}")
        print()

        return self._compute_results()

    def _compute_results(self) -> Dict[str, Any]:
        eq_df = pd.DataFrame(self._equity_curve)
        if eq_df.empty:
            return {"error": "No data"}

        equity = eq_df["total_value"]
        returns = equity.pct_change().dropna()
        total_ret = (equity.iloc[-1] / equity.iloc[0]) - 1

        periods_per_year = 365 * 6
        try:
            ts = pd.to_datetime(eq_df["timestamp"])
            gap = ts.diff().dropna().median().total_seconds() / 3600
            if gap > 18:
                periods_per_year = 365
            elif gap > 2:
                periods_per_year = 365 * 6
        except Exception:
            pass

        n_p = len(returns)
        ann_ret = (1 + total_ret) ** (periods_per_year / max(n_p, 1)) - 1
        vol = returns.std() * np.sqrt(periods_per_year)
        rf = 0.05 / periods_per_year
        sharpe = float((returns.mean() - rf) / returns.std() * np.sqrt(periods_per_year)) if returns.std() > 0 else 0

        cum = (1 + returns).cumprod()
        max_dd = abs(((cum - cum.cummax()) / cum.cummax()).min())

        dn = returns[returns < 0]
        dn_vol = dn.std() * np.sqrt(periods_per_year) if len(dn) > 0 else 0
        sortino = float((ann_ret - 0.05) / dn_vol) if dn_vol > 0 else 0
        calmar = float(ann_ret / max_dd) if max_dd > 0 else 0

        closed = [t for t in self._trades if t["action"].startswith("CLOSE")]
        wins = [t for t in closed if t.get("pnl", 0) > 0]
        losses = [t for t in closed if t.get("pnl", 0) <= 0]
        total_fees = sum(t.get("fee", 0) for t in self._trades)

        win_rate = len(wins) / max(len(closed), 1)
        avg_win = float(np.mean([t["pnl"] for t in wins])) if wins else 0
        avg_loss = float(np.mean([t["pnl"] for t in losses])) if losses else 0
        pf = abs(sum(t["pnl"] for t in wins)) / max(abs(sum(t["pnl"] for t in losses)), 1)
        avg_bars = float(np.mean([t.get("bars_held", 0) for t in closed])) if closed else 0

        reasons = {}
        for t in closed:
            reasons[t.get("reason", "?")] = reasons.get(t.get("reason", "?"), 0) + 1

        try:
            eq2 = eq_df.copy()
            eq2["timestamp"] = pd.to_datetime(eq2["timestamp"])
            eq2.set_index("timestamp", inplace=True)
            monthly = eq2["total_value"].resample("ME").last().pct_change().dropna()
            m_hit = float((monthly > 0).mean())
            best_m = float(monthly.max() * 100)
            worst_m = float(monthly.min() * 100)
            n_months = len(monthly)
        except Exception:
            m_hit, best_m, worst_m, n_months = 0, 0, 0, 0

        return {
            "metrics": {
                "total_return_pct": round(total_ret * 100, 2),
                "annual_return_pct": round(ann_ret * 100, 2),
                "annual_volatility": round(vol, 4),
                "sharpe_ratio": round(sharpe, 3),
                "sortino_ratio": round(sortino, 3),
                "calmar_ratio": round(calmar, 3),
                "max_drawdown_pct": round(max_dd * 100, 2),
                "win_rate": round(win_rate, 4),
                "profit_factor": round(pf, 3),
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "avg_bars_held": round(avg_bars, 1),
                "total_trades": len(closed),
                "total_fees": round(total_fees, 2),
                "monthly_hit_rate": round(m_hit, 4),
                "total_months": n_months,
                "best_month_pct": round(best_m, 2),
                "worst_month_pct": round(worst_m, 2),
                "start_value": round(float(equity.iloc[0]), 2),
                "end_value": round(float(equity.iloc[-1]), 2),
                "exit_reasons": reasons,
            },
            "equity_curve": self._equity_curve,
            "trades": self._trades,
        }


# ────────────────────────────────────────────────────────────
#  Report & Sweep
# ────────────────────────────────────────────────────────────

def print_report(results: Dict):
    m = results.get("metrics", {})
    trades = results.get("trades", [])

    print(f"\n{'=' * 68}")
    print(f"  CRYPTO PERPETUAL ALPHA MACHINE (CPAM) — REPORT")
    print(f"{'=' * 68}")
    print(f"  Total Return:      {m.get('total_return_pct', 0):+.2f}%")
    print(f"  Annual Return:     {m.get('annual_return_pct', 0):+.2f}%")
    print(f"  Sharpe Ratio:      {m.get('sharpe_ratio', 0):.3f}")
    print(f"  Sortino Ratio:     {m.get('sortino_ratio', 0):.3f}")
    print(f"  Calmar Ratio:      {m.get('calmar_ratio', 0):.3f}")
    print(f"  Max Drawdown:      {m.get('max_drawdown_pct', 0):.2f}%")
    print(f"  Annual Volatility: {m.get('annual_volatility', 0):.4f}")
    print(f"  Start → End:       ${m.get('start_value', 0):,.2f} → ${m.get('end_value', 0):,.2f}")
    print(f"{'=' * 68}")
    print(f"  Total Trades:      {m.get('total_trades', 0)}")
    print(f"  Win Rate:          {m.get('win_rate', 0) * 100:.1f}%")
    print(f"  Profit Factor:     {m.get('profit_factor', 0):.2f}")
    print(f"  Avg Win:           ${m.get('avg_win', 0):+.2f}")
    print(f"  Avg Loss:          ${m.get('avg_loss', 0):+.2f}")
    print(f"  Avg Bars Held:     {m.get('avg_bars_held', 0):.1f} ({m.get('avg_bars_held', 0) * 4:.0f}h)")
    print(f"  Total Fees:        ${m.get('total_fees', 0):,.2f}")
    print(f"  Monthly Hit Rate:  {m.get('monthly_hit_rate', 0) * 100:.1f}% ({m.get('total_months', 0)} mo)")
    print(f"  Best Month:        {m.get('best_month_pct', 0):+.2f}%")
    print(f"  Worst Month:       {m.get('worst_month_pct', 0):+.2f}%")
    print(f"{'=' * 68}")

    reasons = m.get("exit_reasons", {})
    if reasons:
        print(f"  Exit Reasons:")
        for r, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    {r:20s}: {cnt}")
    print(f"{'=' * 68}")

    closed = [t for t in trades if t["action"].startswith("CLOSE")]
    if closed:
        print(f"\n  Last 20 Trades:")
        for t in closed[-20:]:
            pnl = t.get("pnl", 0)
            pnl_pct = t.get("pnl_pct", 0)
            bars = t.get("bars_held", 0)
            print(f"    {t['timestamp'][:16]}  {t['action']:12s}  "
                  f"entry={t.get('entry_price', 0):>10.1f}  "
                  f"exit={t.get('price', 0):>10.1f}  "
                  f"PnL={pnl:+10.2f} ({pnl_pct:+5.1f}%)  "
                  f"{bars:2d}bars  {t.get('reason', 'n/a')}")
    print()


def run_parameter_sweep(df: pd.DataFrame, macro_df: pd.DataFrame, capital: float):
    configs = [
        {"name": "Baseline",
         "leverage": 10.0},
        {"name": "HighML",
         "leverage": 10.0, "ml_weight": 0.55, "macro_weight": 0.25, "ts_weight": 0.20},
        {"name": "HighMacro",
         "leverage": 10.0, "ml_weight": 0.30, "macro_weight": 0.50, "ts_weight": 0.20},
        {"name": "WideStop",
         "leverage": 10.0, "atr_stop_mult": 3.0, "atr_tp_mult": 6.0,
         "trailing_stop_atr": 3.5},
        {"name": "TightEntry",
         "leverage": 10.0, "entry_threshold": 0.35, "strong_entry": 0.55},
        {"name": "LooseEntry",
         "leverage": 10.0, "entry_threshold": 0.15, "strong_entry": 0.35},
        {"name": "Conservative",
         "leverage": 7.0, "base_position_pct": 0.08, "atr_stop_mult": 3.0},
        {"name": "Aggressive",
         "leverage": 12.0, "base_position_pct": 0.12, "atr_stop_mult": 2.0},
    ]

    print(f"\n{'=' * 110}")
    print(f"  CPAM STRATEGY — PARAMETER SWEEP ({len(configs)} configs)")
    print(f"{'=' * 110}")
    hdr = (f"  {'Config':<14s} {'Return':>8s} {'AnnRet':>8s} {'Sharpe':>8s} "
           f"{'MaxDD':>8s} {'WinRate':>8s} {'PF':>6s} {'Trades':>7s} "
           f"{'MoHit':>7s} {'EndVal':>10s}")
    print(hdr)
    print(f"  {'-' * 104}")

    all_res = []
    for cfg in configs:
        name = cfg.pop("name")
        bt = CryptoAlphaBacktest(initial_capital=capital, **cfg)
        result = bt.run(df, macro_df)
        m = result.get("metrics", {})

        print(f"  {name:<14s} {m.get('total_return_pct', 0):>+7.1f}% "
              f"{m.get('annual_return_pct', 0):>+7.1f}% "
              f"{m.get('sharpe_ratio', 0):>8.3f} "
              f"{m.get('max_drawdown_pct', 0):>7.1f}% "
              f"{m.get('win_rate', 0) * 100:>7.1f}% "
              f"{m.get('profit_factor', 0):>5.2f} "
              f"{m.get('total_trades', 0):>7d} "
              f"{m.get('monthly_hit_rate', 0) * 100:>6.1f}% "
              f"${m.get('end_value', 0):>9,.0f}")

        all_res.append({"name": name, **m})

    print(f"  {'-' * 104}")
    valid = [r for r in all_res if r.get("total_return_pct", 0) > 0]
    if valid:
        best = max(valid, key=lambda x: x.get("sharpe_ratio", -999))
        print(f"\n  Best Sharpe: {best['name']} "
              f"(Sharpe={best.get('sharpe_ratio', 0):.3f}, "
              f"Return={best.get('total_return_pct', 0):+.1f}%)")
    print()
    return all_res


# ────────────────────────────────────────────────────────────
#  Main
# ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CPAM Strategy Backtest")
    parser.add_argument("--data", type=str, default=None)
    parser.add_argument("--symbol", type=str, default="BTC/USDT")
    parser.add_argument("--capital", type=float, default=10000)
    parser.add_argument("--leverage", type=float, default=10.0)
    parser.add_argument("--sweep", action="store_true")
    parser.add_argument("--save", type=str, default=None)
    parser.add_argument("--start", type=str, default=None)
    parser.add_argument("--end", type=str, default=None)
    parser.add_argument("--no-macro", action="store_true", help="Skip macro data fetch")
    args = parser.parse_args()

    # Find data
    data_file = args.data
    if not data_file:
        safe = args.symbol.replace("/", "_")
        for c in [PROJECT_ROOT / "data" / "sample" / f"{safe}_4h.csv",
                   PROJECT_ROOT / "data" / "crypto" / f"{safe}_4h.csv"]:
            if c.exists():
                data_file = str(c)
                break

    if not data_file or not Path(data_file).exists():
        print(f"ERROR: No data for {args.symbol}")
        sys.exit(1)

    print(f"Loading: {data_file}")
    df = pd.read_csv(data_file)
    df.columns = [c.lower() for c in df.columns]
    for col in ("timestamp", "time", "datetime", "date"):
        if col in df.columns:
            df.rename(columns={col: "timestamp"}, inplace=True)
            break
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_localize(None)
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)

    if args.start:
        df = df[df["timestamp"] >= pd.Timestamp(args.start)]
    if args.end:
        df = df[df["timestamp"] <= pd.Timestamp(args.end)]
    df.reset_index(drop=True, inplace=True)

    print(f"Shape: {df.shape} | {df['timestamp'].iloc[0]} → {df['timestamp'].iloc[-1]}")

    # Fetch macro data
    macro_df = pd.DataFrame()
    if not args.no_macro:
        print("\nFetching macro data (DXY/SPX/Gold/VIX) via yfinance...")
        start_date = str(df["timestamp"].iloc[0].date() - pd.Timedelta(days=90))
        end_date = str(df["timestamp"].iloc[-1].date() + pd.Timedelta(days=1))
        macro_df = fetch_macro_data(start_date, end_date)
        if not macro_df.empty:
            print(f"  Macro data: {len(macro_df)} days, cols={list(macro_df.columns)}")
        else:
            print("  [WARN] No macro data fetched, proceeding without")

    if args.sweep:
        run_parameter_sweep(df, macro_df, capital=args.capital)
    else:
        bt = CryptoAlphaBacktest(initial_capital=args.capital, leverage=args.leverage)
        results = bt.run(df, macro_df)
        print_report(results)

        if args.save:
            p = Path(args.save)
            p.parent.mkdir(parents=True, exist_ok=True)
            save_data = {
                "strategy": "crypto_perp_alpha_machine",
                "symbol": args.symbol,
                "metrics": results["metrics"],
                "trades": results["trades"],
                "n_equity_points": len(results["equity_curve"]),
            }
            with open(p, "w") as f:
                json.dump(save_data, f, indent=2, default=str)
            print(f"Saved: {p}")


if __name__ == "__main__":
    main()
