#!/usr/bin/env python3
"""Tokenized-stock 20x intraday trend scalp — v0 MECHANICS CHECK.

NOT a validation (12 days of history — the owner explicitly waived the
1.5 gate for this opportunistic sleeve, substituting risk rails).
Purpose: calibrate the machine before small-size execution —
  * do tight stops survive 5m noise at 20x (liquidation-clamped)?
  * what do fees do at scalp frequency?
  * which names/params are outright unworkable?

Engine: SniperV3 (honest accounting + stops clamped inside liquidation),
leverage=20, on catalog 5m bars (collector keeps them fresh).

Run:  python scripts/backtest_tokenized_scalp_v0.py
"""
from __future__ import annotations

import itertools
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

t0 = time.time()


def say(msg: str) -> None:
    print(f"[{time.time()-t0:5.1f}s] {msg}", flush=True)


say("imports...")
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from data.catalog_api import catalog  # noqa: E402
from scripts.redesign_sniper_v3 import SniperV3  # noqa: E402

NAMES = ["SPCX", "NVDA", "TSLA", "HOOD"]      # survives-20x singles
LEV = 20.0
RISK = 0.01          # 1% per trade of the SLEEVE (sleeve itself is small)
GRID = {
    "atr_stop": [1.0, 1.5, 2.0],
    "atr_trail": [1.0, 1.5],
    "min_score": [2],
    "max_hold": [24, 72],       # 2h / 6h in 5m bars
    "vol_min": [1.2],
    "atr_trail_activate": [0.5],
}


def run_one(df: pd.DataFrame, **params) -> dict:
    bt = SniperV3(initial_capital=10_000, leverage=LEV,
                  risk_per_trade=RISK, fee_rate=0.0005, **params)
    bt.run(df.copy())
    ec = pd.DataFrame(bt._equity_curve)
    if ec.empty:
        return {}
    eq = ec["equity"]
    closes = [t for t in bt._trades if t["action"].startswith("CLOSE")]
    wins = [t for t in closes if t["pnl"] > 0]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = -sum(t["pnl"] for t in closes if t["pnl"] <= 0)
    return {
        "final": round(float(eq.iloc[-1]), 0),
        "ret_pct": round(float(eq.iloc[-1] / 10_000 - 1) * 100, 1),
        "max_dd_pct": round(float((eq / eq.cummax() - 1).min()) * 100, 1),
        "n_trades": len(closes),
        "win_rate": round(len(wins) / len(closes), 2) if closes else None,
        "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else None,
        "liq_exits": sum(1 for t in closes if t["reason"] == "liquidation"),
        "stop_exits": sum(1 for t in closes if t["reason"] == "stop_loss"),
        "trail_exits": sum(1 for t in closes if t["reason"] == "trailing_stop"),
    }


def main() -> None:
    results = []
    for base in NAMES:
        try:
            df = catalog.read("tokenized_stock", base, "5m")
        except Exception as e:
            say(f"{base}: no catalog data ({e}) — run the collector first")
            continue
        df.columns = [c.lower() for c in df.columns]
        say(f"{base}: {len(df)} x 5m bars "
            f"({df['timestamp'].min()} -> {df['timestamp'].max()})")
        keys = list(GRID)
        for vals in itertools.product(*GRID.values()):
            params = dict(zip(keys, vals))
            r = run_one(df, **params)
            if r:
                results.append({"name": base, **params, **r})

    if not results:
        say("no results — collector data missing")
        return
    out = pd.DataFrame(results)
    out.to_csv(ROOT / "results" / "tokenized_scalp_v0_mechanics.csv", index=False)

    say("\n=== per-name best by profit_factor (mechanics, 12d, NOT validation) ===")
    for base in NAMES:
        sub = out[out["name"] == base].dropna(subset=["profit_factor"])
        if sub.empty:
            continue
        b = sub.sort_values("profit_factor", ascending=False).iloc[0]
        print(f"{base}: stop={b.atr_stop} trail={b.atr_trail} hold={b.max_hold} "
              f"| ret={b.ret_pct}% dd={b.max_dd_pct}% n={b.n_trades} "
              f"wr={b.win_rate} pf={b.profit_factor} "
              f"exits[stop/trail/liq]={b.stop_exits}/{b.trail_exits}/{b.liq_exits}",
              flush=True)
    say("\n=== aggregate red flags ===")
    print(f"configs with ANY liquidation exit: "
          f"{(out['liq_exits'] > 0).sum()}/{len(out)}", flush=True)
    print(f"median trades per config-name (12d): {out['n_trades'].median():.0f}",
          flush=True)
    say("saved -> results/tokenized_scalp_v0_mechanics.csv")


if __name__ == "__main__":
    main()
