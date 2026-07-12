"""Cross-sectional factor computation, z-scoring, and composite scoring.

This module is the core of the sector-rotation alpha engine. Given a
multi-ticker panel of daily OHLCV, it produces:

  1. A factor panel: date × ticker × factor (raw factor values)
  2. A z-score panel: same shape but z-scored cross-sectionally per day
  3. A composite score: date × ticker, weighted sum of z-scores using
     regime-conditional weights

The same panel can drive backtests at any rebalance date by simply
slicing to that date's row.

Factor convention (positive = bullish for the ticker):

  mom_12_1     = log(close[t-21] / close[t-252])     # 12m ex last month
  mom_6_1      = log(close[t-21] / close[t-126])     # 6m ex last month
  st_reversal  = -log(close[t] / close[t-21])        # 1m return, FLIPPED
  trend_sma200 = (close[t] - sma200[t]) / sma200[t]
  vol_60d      = -realized_vol_60d                   # FLIPPED (low vol = high score)
  beta_spy     = -rolling_beta_252d                  # FLIPPED (low beta = high score)
  div_yield    = trailing dividend yield (decimal)

For carrier ETFs/sectors lacking dividend data, div_yield = 0.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd


FACTOR_NAMES: List[str] = [
    "mom_12_1",
    "mom_6_1",
    "st_reversal",
    "trend_sma200",
    "vol_60d",
    "beta_spy",
    "div_yield",
]


def _wide_close(panel: pd.DataFrame) -> pd.DataFrame:
    """Return a wide DataFrame indexed by timestamp, columns = tickers, values = close."""
    return panel.pivot_table(
        index="timestamp", columns="ticker", values="close", aggfunc="last",
    ).sort_index()


def compute_factor_panel(
    panel: pd.DataFrame,
    universe: List[str],
    *,
    benchmark_ticker: str = "SPY",
    div_yields: Optional[Dict[str, float]] = None,
) -> pd.DataFrame:
    """Compute the 7 factors for every (date, ticker) in ``universe``.

    Args:
        panel: long-format OHLCV panel (timestamp, ticker, ohlcv).
        universe: list of tickers we'll score (the buyable ETFs).
        benchmark_ticker: used for beta calculation (default 'SPY').
        div_yields: dict ticker → trailing dividend yield (decimal).
                    If None, all yields default to 0 (factor is still
                    included so factor weights are stable across regimes).

    Returns:
        A long-format DataFrame:
          [timestamp, ticker, mom_12_1, mom_6_1, ..., div_yield]
        with one row per (date, ticker).
    """
    closes = _wide_close(panel)
    if benchmark_ticker not in closes.columns:
        raise ValueError(f"Benchmark {benchmark_ticker} missing from panel")

    bench = closes[benchmark_ticker]
    bench_log_ret = np.log(bench / bench.shift(1))

    div_yields = div_yields or {}

    frames = []
    for ticker in universe:
        if ticker not in closes.columns:
            # Ticker not in panel — skip gracefully (universe may include
            # newer ETFs that don't have history yet)
            continue
        c = closes[ticker]

        # Momentum 12-1 (skip last month)
        mom_12_1 = np.log(c.shift(21) / c.shift(252))

        # Momentum 6-1
        mom_6_1 = np.log(c.shift(21) / c.shift(126))

        # Short-term reversal (negative of last 21d log return)
        st_reversal = -np.log(c / c.shift(21))

        # Trend vs 200-day SMA (% above)
        sma200 = c.rolling(200, min_periods=100).mean()
        trend_sma200 = (c - sma200) / sma200

        # 60-day annualized realized vol (sign-flipped)
        log_ret = np.log(c / c.shift(1))
        rv_60 = log_ret.rolling(60, min_periods=30).std() * np.sqrt(252)
        vol_60d = -rv_60

        # Rolling 252-day beta vs benchmark (sign-flipped)
        # cov(r_t, r_b) / var(r_b)
        cov_tb = log_ret.rolling(252, min_periods=126).cov(bench_log_ret)
        var_b = bench_log_ret.rolling(252, min_periods=126).var().replace(0, np.nan)
        beta_spy = -(cov_tb / var_b)

        # Static dividend yield (use provided dict)
        dy = div_yields.get(ticker, 0.0)

        df_t = pd.DataFrame({
            "timestamp": c.index,
            "ticker": ticker,
            "mom_12_1": mom_12_1.values,
            "mom_6_1": mom_6_1.values,
            "st_reversal": st_reversal.values,
            "trend_sma200": trend_sma200.values,
            "vol_60d": vol_60d.values,
            "beta_spy": beta_spy.values,
            "div_yield": dy,
        })
        frames.append(df_t)

    if not frames:
        return pd.DataFrame(columns=["timestamp", "ticker"] + FACTOR_NAMES)

    return pd.concat(frames, ignore_index=True).sort_values(
        ["timestamp", "ticker"]
    ).reset_index(drop=True)


def cross_sectional_zscore(
    factor_panel: pd.DataFrame,
    *,
    factor_cols: Optional[List[str]] = None,
    clip: float = 3.0,
) -> pd.DataFrame:
    """Z-score every factor column WITHIN each timestamp (cross-sectional).

    NaN-tolerant: a row with NaN in some factors is left as NaN (not
    pulled toward the cross-sectional mean).

    Args:
        factor_panel: long format from compute_factor_panel.
        factor_cols: which columns to z-score (default: all in FACTOR_NAMES).
        clip: clip z-scores to ±clip to dampen outlier tickers.

    Returns:
        Same shape, factor columns replaced by their z-scores.
    """
    factor_cols = factor_cols or [c for c in FACTOR_NAMES if c in factor_panel.columns]

    z = factor_panel.copy()

    def _z(group: pd.DataFrame) -> pd.DataFrame:
        for col in factor_cols:
            v = group[col]
            mean = v.mean(skipna=True)
            std = v.std(skipna=True)
            if std == 0 or np.isnan(std):
                group[col] = 0.0
            else:
                group[col] = ((v - mean) / std).clip(-clip, clip)
        return group

    return z.groupby("timestamp", group_keys=False).apply(_z)


def composite_score_per_regime(
    z_panel: pd.DataFrame,
    regime_label_per_date: pd.Series,
    regime_factor_weights: Dict[str, Dict[str, float]],
) -> pd.DataFrame:
    """Compute composite score per (date, ticker) using regime-specific weights.

    Args:
        z_panel: cross-sectional z-score panel (long format).
        regime_label_per_date: pd.Series indexed by timestamp, values are
                               regime labels (strings: 'bull', 'bear', etc.).
        regime_factor_weights: dict regime_label → {factor_name → weight}.

    Returns:
        DataFrame with columns: [timestamp, ticker, regime, composite_score].
    """
    z = z_panel.copy()
    z["regime"] = z["timestamp"].map(regime_label_per_date)
    z = z.dropna(subset=["regime"])

    # Compute composite per row by looking up weights for that row's regime
    factor_cols = [c for c in z.columns if c not in ("timestamp", "ticker", "regime")]

    def _score(row: pd.Series) -> float:
        weights = regime_factor_weights.get(row["regime"], {})
        if not weights:
            return 0.0
        s = 0.0
        for f in factor_cols:
            w = weights.get(f, 0.0)
            v = row[f]
            if pd.isna(v):
                continue
            s += w * v
        return s

    z["composite_score"] = z.apply(_score, axis=1)
    return z[["timestamp", "ticker", "regime", "composite_score"] + factor_cols]
