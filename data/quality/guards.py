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
    intraday: bool = False,
) -> tuple:
    """Validate a daily bar panel before research/backtest consumption.

    Returns (clean_df, BarReport). Raises ValueError only on catastrophic
    states (empty, non-datetime index, all-NaN column).

    intraday=True keeps sub-daily timestamps (to_utc_naive, no midnight
    normalization) — required for 1m/1h/4h bars, where normalizing would
    collapse every bar of a day into a "duplicate".
    """
    rep = BarReport(name=name)
    if df is None or len(df) == 0:
        raise ValueError(f"[bars:{name}] empty panel")
    if not isinstance(df.index, pd.DatetimeIndex):
        raise ValueError(f"[bars:{name}] index is not DatetimeIndex")

    df = to_utc_naive(df) if intraday else ensure_utc_naive(df)
    before = len(df)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    rep.dropped_dupes = before - len(df)
    rep.n_rows = len(df)
    if rep.dropped_dupes:
        record_correction(name, "dropped_duplicates",
                          f"{rep.dropped_dupes} duplicate timestamps removed",
                          rep.dropped_dupes)

    # calendar gaps (weekend-tolerant)
    diffs = df.index.to_series().diff().dt.days.dropna()
    gaps = diffs[diffs > max_gap_days]
    for ts, d in gaps.items():
        rep.issues.append(f"gap {int(d)}d ending {ts.date()}")
        record_correction(name, "gap", f"{int(d)}d gap ending {ts.date()}")

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


# ────────────────────────────────────────────────────────────
# Week-3: correction ledger — every silent "fix" leaves a trail
# ────────────────────────────────────────────────────────────

_LEDGER_PATH = None  # resolved lazily so tests can redirect it


def _ledger_path():
    global _LEDGER_PATH
    if _LEDGER_PATH is None:
        from pathlib import Path
        _LEDGER_PATH = (Path(__file__).resolve().parents[1]
                        / "quality" / "corrections.jsonl")
    return _LEDGER_PATH


def record_correction(source: str, kind: str, detail: str,
                      n_affected: int = 1) -> None:
    """Append one correction/degradation event to the audit ledger.

    kind: dropped_duplicates | gap | nan_column | stale | chain_anomaly | fix
    Never raises — the ledger must not be able to break a data path.
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    try:
        p = _ledger_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(_json.dumps({
                "ts": _dt.now(_tz.utc).isoformat(timespec="seconds"),
                "source": source, "kind": kind,
                "detail": detail, "n_affected": n_affected,
            }) + "\n")
    except Exception:
        pass


# ────────────────────────────────────────────────────────────
# Week-3: point-in-time join — alt-data may only be seen after its lag
# ────────────────────────────────────────────────────────────

def pit_join(left: pd.DataFrame, right: pd.DataFrame,
             lag_days: int = 1, suffix: str = "_pit") -> pd.DataFrame:
    """As-of join with an availability lag: a right-row stamped at time T
    becomes visible to the left timeline only at T + lag_days.

    Both frames must be datetime-indexed (daily). This is THE sanctioned way
    to attach alt-data (filings, fundamentals, vendor snapshots) to a price
    timeline — a plain merge on date is look-ahead by construction.
    """
    left = ensure_utc_naive(left).sort_index()
    right = ensure_utc_naive(right).sort_index()
    shifted = right.copy()
    shifted.index = shifted.index + pd.Timedelta(days=lag_days)
    out = pd.merge_asof(
        left, shifted.add_suffix(suffix),
        left_index=True, right_index=True, direction="backward",
    )
    return out


# ────────────────────────────────────────────────────────────
# Week-3: dataset manifests — pin exactly which bytes a backtest consumed
# ────────────────────────────────────────────────────────────

def record_data_manifest(paths, tag: str) -> "Path":
    """Hash the input files of a run and persist a manifest JSON.

    Returns the manifest path; embed it (or its sha field) in result files so
    any backtest number can be traced to the exact input bytes that made it.
    """
    import hashlib
    import json as _json
    from datetime import datetime as _dt, timezone as _tz
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    out_dir = root / "data" / "snapshots"
    out_dir.mkdir(parents=True, exist_ok=True)

    entries = []
    for p in map(Path, paths):
        e = {"file": str(p)}
        try:
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            e["sha256"] = h.hexdigest()
            e["bytes"] = p.stat().st_size
        except Exception as ex:
            e["error"] = f"{type(ex).__name__}: {ex}"
        entries.append(e)

    import uuid
    ts = _dt.now(_tz.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest = {"tag": tag, "created": ts, "inputs": entries}
    # uuid suffix: same-second runs must not overwrite each other's manifest
    out = out_dir / f"manifest_{tag}_{ts}_{uuid.uuid4().hex[:6]}.json"
    out.write_text(_json.dumps(manifest, indent=1))
    return out
