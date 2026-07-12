#!/usr/bin/env python3
"""Parity check: incremental SniperSignals vs the reference pandas
compute_signals() from scripts/backtest_btc_sniper.py, on the actual ETH CSV.

Signal-level parity means any backtest-result gap is attributable to fill /
accounting semantics, not indicator drift.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from backtest_btc_sniper import compute_signals  # noqa: E402
from eth_sniper_nautilus import SniperSignals  # noqa: E402
from load_data import load_dataframe  # noqa: E402


def main() -> None:
    df = load_dataframe().reset_index()
    ref = compute_signals(df)

    inc = SniperSignals()
    rows = []
    for h, l, c, v in zip(df["high"], df["low"], df["close"], df["volume"]):
        rows.append(inc.update(float(h), float(l), float(c), float(v)))

    keys = [
        "ema8", "ema21", "ema55", "ema55_slope", "rsi", "macd_hist", "atr",
        "adx", "di_spread", "dc_high_prev10", "dc_low_prev10", "vol_ratio",
    ]
    warmup = 60
    print(f"{'signal':<16s} {'max_abs_diff(post60)':>22s} {'max_rel_diff':>14s}")
    ok = True
    for k in keys:
        mine = np.array([r[k] for r in rows], dtype=float)
        theirs = ref[k].to_numpy(dtype=float)
        if k == "rsi":
            theirs = np.where(np.isnan(theirs), 50.0, theirs)
        m, t = mine[warmup:], theirs[warmup:]
        both = ~(np.isnan(m) | np.isnan(t))
        nan_mismatch = int((np.isnan(m) != np.isnan(t)).sum())
        diff = np.abs(m[both] - t[both])
        rel = diff / np.maximum(np.abs(t[both]), 1e-9)
        status = ""
        if nan_mismatch or (len(diff) and diff.max() > 1e-6):
            ok = False
            status = f"  <-- MISMATCH (nan_mismatch={nan_mismatch})"
        print(f"{k:<16s} {diff.max() if len(diff) else 0:>22.3e} "
              f"{rel.max() if len(rel) else 0:>14.3e}{status}")

    # Boolean flags
    for k in ("near_ema21_up", "near_ema21_dn"):
        mine = np.array([bool(r[k]) for r in rows])
        theirs = ref[k].to_numpy(dtype=bool)
        n_bad = int((mine[warmup:] != theirs[warmup:]).sum())
        if n_bad:
            ok = False
        print(f"{k:<16s} mismatches post-warmup: {n_bad}")

    print("\nPARITY:", "PASS" if ok else "FAIL")


if __name__ == "__main__":
    main()
