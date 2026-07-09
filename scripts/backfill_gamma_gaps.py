#!/usr/bin/env python3
"""Backfill the paper-trading gaps: replay Gamma Scalp over the FULL calendar
(no missed days) and diff against the actual paper log.

The local scheduler missed ~16 trading days (machine off):
  4-15→4-20, 5-08→5-13, 6-17→7-02.
Question: did the strategy miss any entries/exits during those windows, and
where would equity be with 100% uptime?
"""
from __future__ import annotations
import json, sys, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import yfinance as yf
from data.quality.guards import ensure_utc_naive, to_utc_naive
import yaml
from scripts.backtest_gamma_scalp import run_backtest_sync

PAPER_START = "2026-03-19"
GAPS = [("2026-04-15", "2026-04-20"), ("2026-05-08", "2026-05-13"),
        ("2026-06-17", "2026-07-02")]

# ── 1. Fetch full data (warmup from 2025-10 for 60d+ indicator history) ──
print("Fetching SPY/VIX/IRX 2025-10-01 → today...")
spy = yf.Ticker("SPY").history(start="2025-10-01", interval="1d")
vix = yf.Ticker("^VIX").history(start="2025-10-01", interval="1d")
irx = yf.Ticker("^IRX").history(start="2025-10-01", interval="1d")
for df_ in (spy, vix, irx):
    df_.index = ensure_utc_naive(df_.index)

df = pd.DataFrame({
    "open": spy["Open"], "high": spy["High"], "low": spy["Low"],
    "close": spy["Close"], "volume": spy["Volume"],
    "vix_close": vix["Close"].reindex(spy.index).ffill(),
    "irx_close": irx["Close"].reindex(spy.index).ffill(),
})
df["implied_vol"] = df["vix_close"] / 100.0
df["risk_free_rate"] = df["irx_close"] / 100.0
df = df.dropna(subset=["close"]).reset_index().rename(columns={"index": "timestamp", "Date": "timestamp"})
print(f"  {len(df)} bars, {df['timestamp'].iloc[0].date()} → {df['timestamp'].iloc[-1].date()}")

# ── 2. Replay with the paper trader's exact params ──
cfg = yaml.safe_load(open(ROOT / "config/strategies/spy_gamma_scalp_optimized.yaml"))
params = cfg.get("parameters", {})
res = run_backtest_sync(df, 100_000, params)

# ── 3. Per-day actions within paper window ──
eq = pd.DataFrame(res["equity_curve"])
eq["timestamp"] = pd.to_datetime(eq["timestamp"])
eq = eq[eq["timestamp"] >= PAPER_START].set_index("timestamp")

trades = res["trade_results"]
print(f"\n=== FULL-UPTIME REPLAY ({PAPER_START} → today) ===")
print(f"Final equity: ${eq['total_value'].iloc[-1]:,.2f}  "
      f"(actual paper: $100,254.33)")
print(f"Trades: {len([t for t in trades if pd.Timestamp(t['timestamp']) >= pd.Timestamp(PAPER_START)])} "
      f"(actual paper: 1)")
print("\nAll replay trades in paper window:")
for t in trades:
    ts = pd.Timestamp(t["timestamp"])
    if ts < pd.Timestamp(PAPER_START):
        continue
    in_gap = any(pd.Timestamp(a) <= ts <= pd.Timestamp(b) for a, b in GAPS)
    flag = "  ← ⚠️ IN GAP (missed by paper)" if in_gap else ""
    print(f"  {str(ts.date())} {t['direction']:8s} exit_reason={t['reason']:14s} "
          f"pnl=${t['total_pnl']:+8,.0f}{flag}")

# ── 4. What happened inside each gap window ──
print("\n=== GAP WINDOWS DETAIL ===")
for a, b in GAPS:
    win = eq[(eq.index >= a) & (eq.index <= b)]
    if win.empty:
        print(f"  {a} → {b}: no data")
        continue
    pos_days = int((win["n_positions"] > 0).sum())
    eq_chg = win["total_value"].iloc[-1] - win["total_value"].iloc[0]
    print(f"  {a} → {b}: {len(win)} bars, position open {pos_days} days, "
          f"equity Δ ${eq_chg:+,.2f}")

out = ROOT / "results" / "gamma_backfill_replay.json"
json.dump({"final_equity": float(eq["total_value"].iloc[-1]),
           "trades": [{k: (str(v) if isinstance(v, pd.Timestamp) else v)
                       for k, v in t.items()} for t in trades]},
          open(out, "w"), indent=2, default=str)
print(f"\nSaved → {out}")
