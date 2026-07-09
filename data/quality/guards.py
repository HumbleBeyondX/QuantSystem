"""Data-quality guards — Week-1 of the data layer upgrade (2026-07).

Three incident-driven guards (each maps to a real failure that cost us):
  ensure_bars_valid : backtests consumed silently-broken price panels
                      (scheduler gaps, NaN runs, duplicate days) unchecked.
  chain_sanity      : yfinance option chains degraded to OI/IV=0 for ~2 weeks
                      and the gamma-squeeze scanner kept producing GEX=0.
  ensure_utc_naive  : 9+ scripts each reinvented tz normalization; one shared
                      helper so new code stops adding variants.

Design: guards RETURN a report and only raise on catastrophic problems
(empty/duplicated/non-monotonic data). Degradation (gaps, zero-streaks)
is surfaced loudly but lets the caller decide — research code can proceed
with eyes open; live code should treat any `issues` as a stop signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Union

import numpy as np
import pandas as pd


# ────────────────────────────────────────────────────────────
# Timestamps
# ────────────────────────────────────────────────────────────

def to_utc_naive(obj: Union[pd.Series, pd.DataFrame, pd.DatetimeIndex, pd.Timestamp]):
    """tz-aware → tz-naive UTC, PRESERVING intraday times. Use for 1h/4h bars
    and scalar Timestamps. (Daily data should use ensure_utc_naive, which
    also midnight-normalizes.)"""
    if isinstance(obj, pd.Timestamp):
        return obj.tz_convert("UTC").tz_localize(None) if obj.tzinfo else obj
    if isinstance(obj, (pd.Series, pd.DataFrame)):
        obj = obj.copy()
        obj.index = to_utc_naive(obj.index)
        return obj
    idx = pd.DatetimeIndex(obj)
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    return idx


def ensure_utc_naive(obj: Union[pd.Series, pd.DataFrame, pd.DatetimeIndex]):
    """Normalize a datetime index (or the index of a Series/DataFrame) to
    tz-naive UTC, normalized to midnight. THE single sanctioned way to align
    DAILY data — use this instead of ad-hoc `tz_localize(None)` chains."""
    if isinstance(obj, (pd.Series, pd.DataFrame)):
        obj = obj.copy()
        obj.index = ensure_utc_naive(obj.index)
        return obj
    return to_utc_naive(obj).normalize()


# ────────────────────────────────────────────────────────────
# Bar panels (daily OHLCV / close series)
# ────────────────────────────────────────────────────────────

@dataclass
class BarReport:
    name: str
    n_rows: int = 0
    issues: List[str] = field(default_factory=list)   # degradation, non-fatal
    dropped_dupes: int = 0

    @property
    def ok(self) -> bool:
        return not self.issues

    def __str__(self) -> str:
        s = f"[bars:{self.name}] {self.n_rows} rows"
        if self.dropped_dupes:
            s += f", {self.dropped_dupes} dupes dropped"
        return s + (" OK" if self.ok else f" ISSUES: {'; '.join(self.issues)}")


def ensure_bars_valid(
    df: pd.DataFrame,
    name: str = "panel",
    *,
    max_gap_days: int = 7,
    max_nan_frac: float = 0.02,
    stale_after_days: Optional[int] = 5,
) -> tuple:
    """Validate a daily bar panel before research/backtest consumption.

    Returns (clean_df, BarReport). Raises ValueError only on catastrophic
    states (empty, non-datetime index, all-NaN column).
    """
    rep = BarReport(name=name)
    if df is None or len(df) == 0:
        raise ValueError(f"[bars:{name}] empty panel")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError(f"[bars:{name}] index is not DatetimeIndex")

    df = ensure_utc_naive(df)
    before = len(df)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    rep.dropped_dupes = before - len(df)
    rep.n_rows = len(df)

    # calendar gaps (weekend-tolerant)
    diffs = df.index.to_series().diff().dt.days.dropna()
    gaps = diffs[diffs > max_gap_days]
    for ts, d in gaps.items():
        rep.issues.append(f"gap {int(d)}d ending {ts.date()}")

    # NaN density + all-NaN columns
    for col in df.columns:
        frac = float(df[col].isna().mean())
        if frac >= 0.999:
            raise ValueError(f"[bars:{name}] column '{col}' is all-NaN")
        if frac > max_nan_frac:
            rep.issues.append(f"{col}: {frac:.1%} NaN")

    # staleness vs now
    if stale_after_days is not None:
        age = (pd.Timestamp.utcnow().tz_localize(None).normalize()
               - df.index[-1]).days
        if age > stale_after_days:
            rep.issues.append(f"stale: last bar {df.index[-1].date()} ({age}d old)")

    return df, rep


def flat_series(s: pd.Series, window: int = 10, min_std: float = 1e-9) -> bool:
    """True if the last `window` values are suspiciously constant."""
    tail = s.dropna().tail(window)
    return len(tail) >= window and float(tail.std()) <= min_std


# ────────────────────────────────────────────────────────────
# Option chains
# ────────────────────────────────────────────────────────────

@dataclass
class ChainReport:
    symbol: str
    n_contracts: int = 0
    zero_oi_frac: float = 0.0
    zero_iv_frac: float = 0.0
    issues: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def __str__(self) -> str:
        s = (f"[chain:{self.symbol}] {self.n_contracts} contracts, "
             f"zeroOI={self.zero_oi_frac:.0%}, zeroIV={self.zero_iv_frac:.0%}")
        return s + (" OK" if self.ok else f" ISSUES: {'; '.join(self.issues)}")


def chain_sanity(
    chain: pd.DataFrame,
    symbol: str,
    *,
    oi_col: str = "openInterest",
    iv_col: str = "impliedVolatility",
    max_zero_oi: float = 0.90,
    max_zero_iv: float = 0.60,
    min_contracts: int = 10,
) -> ChainReport:
    """Detect the failure mode that poisoned the scanner for two weeks:
    a chain that *parses* fine but is all zeros. Values inside legal ranges
    but suspiciously degenerate across the WHOLE chain."""
    rep = ChainReport(symbol=symbol, n_contracts=len(chain))
    if len(chain) < min_contracts:
        rep.issues.append(f"thin chain ({len(chain)} < {min_contracts})")
        return rep
    if oi_col in chain:
        rep.zero_oi_frac = float((chain[oi_col].fillna(0) <= 0).mean())
        if rep.zero_oi_frac > max_zero_oi:
            rep.issues.append(f"OI degenerate ({rep.zero_oi_frac:.0%} zero)")
    if iv_col in chain:
        rep.zero_iv_frac = float((chain[iv_col].fillna(0) <= 0).mean())
        if rep.zero_iv_frac > max_zero_iv:
            rep.issues.append(f"IV degenerate ({rep.zero_iv_frac:.0%} zero)")
        ivs = chain[iv_col].replace(0, np.nan).dropna()
        if len(ivs) >= min_contracts and float(ivs.std()) < 1e-4:
            rep.issues.append("IV flat across chain (no smile) — vendor glitch?")
    return rep
