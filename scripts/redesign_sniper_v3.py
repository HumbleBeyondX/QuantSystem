#!/usr/bin/env python3
"""ETH Sniper v3 redesign — 50x kept, HONEST margin physics.

Constraint from user: leverage stays 50x. Physics at 50x: liquidation
sits ~1.9% away, so any stop wider than that is fiction (the v1 defect's
hiding place). v3 therefore:
  * stop_dist clamped to 0.9 * price / leverage (fires INSIDE liquidation)
  * full loss accounting (no cap) — inherits the v2 honest _close
  * everything else = the validated engine's mechanics

Anti-overfit protocol: parameters are selected on IS ONLY
(2020..2024-08-31); OOS (2024-09-01..) is only read for the top-5
IS configs. Any survivor must then pass a Nautilus cross-engine check
before entering the registry.
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest_combo_wheel_sniper_v2 import HonestSniperBacktest  # noqa: E402

OOS_START = "2024-09-01"
LIQ_BUFFER = 0.9   # stop must sit inside 90% of liquidation distance


class SniperV3(HonestSniperBacktest):
    """Honest engine + stops clamped inside the 50x liquidation line."""

    def _open(self, price, direction, atr, timestamp, info):
        # temporarily shrink atr_stop so the parent computes a clamped stop
        liq_dist = price * LIQ_BUFFER / self.leverage
        orig = self.atr_stop
        if atr * self.atr_stop > liq_dist:
            self.atr_stop = liq_dist / max(atr, 1e-9)
        try:
            super()._open(price, direction, atr, timestamp, info)
        finally:
            self.atr_stop = orig


def run_config(df: pd.DataFrame, **params) -> dict:
    bt = SniperV3(initial_capital=100_000, leverage=50, **params)
    bt.run(df.copy())
    ec = pd.DataFrame(bt._equity_curve)
    ec["ts"] = pd.to_datetime(ec["ts"])
    eq = ec.set_index("ts")["equity"].resample("D").last().dropna()
    r = eq.pct_change().dropna()

    def st(rr):
        if len(rr) < 60 or rr.std() == 0:
            return {}
        e = (1 + rr).cumprod()
        return {"sharpe": round(float(rr.mean() / rr.std() * np.sqrt(365)), 2),
                "max_dd": round(float((e / e.cummax() - 1).min()), 3),
                "cagr": round(float(e.iloc[-1] ** (365 / len(rr)) - 1), 3)}

    is_mask = r.index < OOS_START
    closes = [t for t in bt._trades if t["action"].startswith("CLOSE")]
    wins = sum(1 for t in closes if t["pnl"] > 0)
    return {"params": params, "is": st(r[is_mask]), "oos": st(r[~is_mask]),
            "full": st(r), "n_trades": len(closes),
            "win_rate": round(wins / len(closes), 3) if closes else None}


def main() -> None:
    df = pd.read_csv(ROOT / "data/sample/ETH_USDT_4h.csv")
    df.columns = [c.lower() for c in df.columns]
    ts = "timestamp" if "timestamp" in df.columns else df.columns[0]
    df[ts] = pd.to_datetime(df[ts], utc=True).dt.tz_localize(None)

    grid = {
        "atr_stop": [0.5, 0.75, 1.0],
        "atr_trail": [0.75, 1.0, 1.5],
        "atr_trail_activate": [0.5],
        "min_score": [2, 3],
        "max_hold": [60, 180],
        "risk_per_trade": [0.05],
        "vol_min": [1.2],
    }
    keys = list(grid)
    results = []
    combos = list(itertools.product(*grid.values()))
    print(f"sweeping {len(combos)} configs (selection on IS ONLY)...")
    for i, vals in enumerate(combos):
        params = dict(zip(keys, vals))
        try:
            res = run_config(df, **params)
            results.append(res)
        except Exception as e:
            results.append({"params": params, "error": str(e)[:80]})
        if (i + 1) % 6 == 0:
            print(f"  {i + 1}/{len(combos)} done")

    ok = [r for r in results if r.get("is", {}).get("sharpe") is not None]
    ok.sort(key=lambda r: r["is"]["sharpe"], reverse=True)
    print(f"\n=== TOP 10 by IS Sharpe (OOS shown only for top 5) ===")
    for i, r in enumerate(ok[:10]):
        p = r["params"]
        line = (f"#{i+1} stop={p['atr_stop']} trail={p['atr_trail']} "
                f"score>={p['min_score']} hold={p['max_hold']} | "
                f"IS {r['is']} | n={r['n_trades']} wr={r['win_rate']}")
        if i < 5:
            line += f" | OOS {r['oos']}"
        print(line)

    out = ROOT / "results" / "sniper_v3_sweep.json"
    out.write_text(json.dumps({"protocol": "IS-selection only; OOS read for top-5",
                               "oos_start": OOS_START, "grid": grid,
                               "results": ok}, indent=1))
    print(f"\nsaved → results/sniper_v3_sweep.json")


if __name__ == "__main__":
    main()
