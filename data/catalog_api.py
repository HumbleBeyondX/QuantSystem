"""Unified data catalog — the shared data spine (2026-07 arch upgrade).

One place where bars live; our engines and NautilusTrader consume the SAME
bytes, manifest-hashed. Layout:

    data/catalog/{asset_class}/{symbol}/{freq}.parquet
    data/catalog/index.json           (inventory: rows, span, sha256, source)

e.g. crypto/ETH_USDT/4h.parquet, equity/SPY/1d.parquet, panel/sp500/1d.parquet

Every write is normalized (lowercase columns, UTC-naive `timestamp`, sorted,
deduped — dupes leave a trail in the correction ledger), validated with
`ensure_bars_valid` when the frame carries OHLC, sha256-manifested via
`record_data_manifest`, and registered in index.json.

Usage:
    from data.catalog_api import catalog
    catalog.write(df, "crypto", "ETH_USDT", "4h", source="okx")
    df = catalog.read("crypto", "ETH_USDT", "4h")
    catalog.export_bars_csv("crypto", "ETH_USDT", "4h", out_path)  # -> nautilus

No nautilus imports here — the pilot env consumes the exported CSV.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import pandas as pd

from data.quality.guards import (
    ensure_bars_valid,
    record_correction,
    record_data_manifest,
    to_utc_naive,
)

_OHLC = {"open", "high", "low", "close"}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class DataCatalog:
    """Parquet catalog rooted at data/catalog/ with a JSON inventory index."""

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root is not None else (
            Path(__file__).resolve().parent / "catalog")

    # ── paths / index ──

    def _path(self, asset_class: str, symbol: str, freq: str) -> Path:
        return self.root / asset_class / symbol / f"{freq}.parquet"

    @property
    def _index_path(self) -> Path:
        return self.root / "index.json"

    def _load_index(self) -> dict:
        if self._index_path.exists():
            return json.loads(self._index_path.read_text())
        return {}

    def _save_index(self, index: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_path.write_text(json.dumps(index, indent=1, default=str))

    # ── core API ──

    def write(self, df: pd.DataFrame, asset_class: str, symbol: str,
              freq: str, source: str) -> dict:
        """Normalize + validate + persist a frame; returns the index entry.

        Normalization: lowercase columns, `timestamp` column UTC-naive
        (intraday times preserved), sorted, deduped on timestamp (plus
        `ticker` for long panels). Duplicate timestamps are dropped
        keep-last and recorded in the correction ledger.
        """
        if df is None or len(df) == 0:
            raise ValueError(f"[catalog:{symbol}/{freq}] refusing empty frame")

        df = df.copy()
        df.columns = [str(c).lower() for c in df.columns]

        # timestamp column — pull from the index if that's where it lives
        if "timestamp" not in df.columns:
            if isinstance(df.index, pd.DatetimeIndex):
                df.index = df.index.rename("timestamp")
                df = df.reset_index()
            else:
                raise ValueError(
                    f"[catalog:{symbol}/{freq}] no 'timestamp' column and "
                    f"index is not DatetimeIndex")
        df["timestamp"] = to_utc_naive(
            pd.DatetimeIndex(pd.to_datetime(df["timestamp"])))

        # sort + dedup (long panels dedup per (timestamp, ticker);
        # trade prints keep legitimate same-timestamp rows)
        subset = ["timestamp"] + (["ticker"] if "ticker" in df.columns else [])
        df = df.sort_values(subset, kind="mergesort").reset_index(drop=True)
        if freq != "trades":
            before = len(df)
            df = df.drop_duplicates(subset=subset, keep="last")
            df = df.reset_index(drop=True)
            dropped = before - len(df)
            if dropped:
                record_correction(
                    f"catalog_{symbol}_{freq}", "dropped_duplicates",
                    f"{dropped} duplicate rows removed on write", dropped)

        # quality gate: bar validation when the frame carries OHLC
        # (long panels have many rows per timestamp — validated per-write
        # for timestamp sanity only, not as a single bar series)
        quality = "no OHLC columns — timestamp normalization only"
        if _OHLC <= set(df.columns) and freq != "trades":
            if "ticker" in df.columns:
                quality = (f"panel: {len(df)} rows, "
                           f"{df['ticker'].nunique()} tickers, "
                           "timestamp-normalized (per-ticker bar validation "
                           "deferred to consumers)")
            else:
                _, rep = ensure_bars_valid(
                    df.set_index("timestamp"),
                    name=f"catalog_{symbol}_{freq}",
                    stale_after_days=None,
                    intraday=(freq not in ("1d", "1w", "1mo")),
                )
                quality = str(rep)

        # persist + hash — ATOMIC: write tmp then rename, so a killed
        # process can never leave a truncated parquet behind
        # (0-byte SPCX/5m.parquet incident, 2026-07-19)
        path = self._path(asset_class, symbol, freq)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        df.to_parquet(tmp, index=False)
        tmp.replace(path)
        sha = _sha256_file(path)
        manifest = record_data_manifest([path], tag=f"catalog_{symbol}_{freq}")

        entry = {
            "symbol": symbol,
            "asset_class": asset_class,
            "freq": freq,
            "rows": int(len(df)),
            "start": str(df["timestamp"].iloc[0]),
            "end": str(df["timestamp"].iloc[-1]),
            "source": source,
            "sha256": sha,
            "written_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"),
            "quality": quality,
            "manifest": str(manifest),
        }
        index = self._load_index()
        index[f"{asset_class}/{symbol}/{freq}"] = entry
        self._save_index(index)
        return entry

    def read(self, asset_class: str, symbol: str, freq: str) -> pd.DataFrame:
        path = self._path(asset_class, symbol, freq)
        if not path.exists():
            raise FileNotFoundError(
                f"[catalog] no entry {asset_class}/{symbol}/{freq} "
                f"(expected {path})")
        return pd.read_parquet(path)

    def list(self) -> List[dict]:
        """All index entries, sorted by (asset_class, symbol, freq)."""
        return sorted(
            self._load_index().values(),
            key=lambda e: (e["asset_class"], e["symbol"], e["freq"]))

    # ── Nautilus export hook (thin — no nautilus imports here) ──

    def export_bars_csv(self, asset_class: str, symbol: str, freq: str,
                        out_path) -> Path:
        """Write the normalized OHLCV csv the nautilus_pilot loader consumes
        (columns: timestamp,open,high,low,close,volume — UTC-naive)."""
        df = self.read(asset_class, symbol, freq)
        cols = ["timestamp", "open", "high", "low", "close", "volume"]
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise ValueError(
                f"[catalog:{symbol}/{freq}] not exportable as bars — "
                f"missing columns {missing}")
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        df[cols].to_csv(out_path, index=False)
        return out_path


# module-level singleton — the sanctioned entry point
catalog = DataCatalog()
