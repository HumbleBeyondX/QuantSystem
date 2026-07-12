#!/usr/bin/env python3
"""Combo re-validation v2 — HONEST sniper leg (liquidation loss cap removed).

Background (2026-07-12, found by the NautilusTrader parity pilot):
backtest_btc_sniper.py caps every losing trade at -margin*0.95
("liquidation") while never actually liquidating positions during the
hold. At 50x with 3.5-ATR stops this halves every realized stop-loss
and inflated the sniper leg from honest Sharpe ~0.42 to reported 2.03.

This script re-runs the combo with the cap removed and re-scores the
official record (results/combo_wheel_sniper_research.json is superseded,
NOT edited — audit trail). Wheel leg is untouched (separate engine,
unaffected by the defect).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.backtest_btc_sniper import BTCSniperBacktest  # noqa: E402

OOS_START = "2024-09-01"


class HonestSniperBacktest(BTCSniperBacktest):
    """Reference engine, verbatim _close() minus ONLY the loss-cap lines."""

    def _close(self, price, reason, timestamp):
        if abs(self._pos) < 1e-12:
            return
        pnl = (price - self._entry) * self._pos
        fee = abs(self._pos * price) * self.fee_rate
        net_pnl = pnl - fee
        # (v1 capped net_pnl at -margin*0.95 here — removed: the engine
        #  never actually liquidates during holds, so the cap only halved
        #  realized stop-losses)
        pnl_pct = net_pnl / max(self._margin, 1) * 100
        equity = self._equity(self._entry)
        pnl_eq_pct = net_pnl / max(equity, 1) * 100

        self._cash += self._margin + net_pnl
        if net_pnl < 0:
            self._consec_loss += 1
        else:
            self._consec_loss = 0

        self._trades.append({
            "ts": timestamp,
            "action": f"CLOSE_{'LONG' if self._pos > 0 else 'SHORT'}",
            "price": round(price, 1),
            "pnl": round(net_pnl, 2),
            "pnl_margin_pct": round(pnl_pct, 1),
            "pnl_equity_pct": round(pnl_eq_pct, 1),
            "fee": round(fee, 2),
            "reason": reason,
            "entry": round(self._entry, 1),
            "bars": self._bars_held,
        })

        self._pos = 0.0
        self._entry = 0.0
        self._margin = 0.0
        self._entry_atr = 0.0
        self._stop_dist = 0.0
        self._bars_held = 0
        self._bars_since = 0


def sniper_daily_equity() -> pd.Series:
    df = pd.read_csv(ROOT / "data/sample/ETH_USDT_4h.csv")
    df.columns = [c.lower() for c in df.columns]
    ts = "timestamp" if "timestamp" in df.columns else df.columns[0]
    df[ts] = pd.to_datetime(df[ts], utc=True).dt.tz_localize(None)
    bt = HonestSniperBacktest(initial_capital=100_000, leverage=50,
                              atr_trail=3.0, max_hold=180)
    bt.run(df)
    ec = pd.DataFrame(bt._equity_curve)
    ec["ts"] = pd.to_datetime(ec["ts"])
    return ec.set_index("ts")["equity"].resample("D").last().dropna()


def stats(r: pd.Series) -> dict:
    if r.std() == 0 or len(r) < 30:
        return {}
    eq = (1 + r).cumprod()
    yrs = len(r) / 365
    return {"sharpe": round(float(r.mean() / r.std() * np.sqrt(365)), 2),
            "max_dd": round(float((eq / eq.cummax() - 1).min()), 4),
            "cagr": round(float(eq.iloc[-1] ** (1 / yrs) - 1), 4)}


def main() -> None:
    wheel_eq = pd.read_csv(ROOT / "results/combo_wheel_sniper_curves.csv",
                           index_col=0, parse_dates=True)["wheel"]
    sn_eq = sniper_daily_equity()
    idx = wheel_eq.index.intersection(sn_eq.index)
    rw = wheel_eq.loc[idx].pct_change().fillna(0)
    rs = sn_eq.loc[idx].pct_change().fillna(0)
    oos = idx >= OOS_START

    out = {
        "id": "combo_wheel_sniper_v2_honest",
        "supersedes": "combo_wheel_sniper (v1 sniper leg inflated by loss-cap defect)",
        "defect": "backtest_btc_sniper.py _close() capped losses at -margin*0.95 "
                  "without ever liquidating during holds",
        "period": {"start": str(idx.min().date()), "end": str(idx.max().date()),
                   "oos_start": OOS_START},
        "correlation": {"full": round(float(rw.corr(rs)), 3),
                        "oos": round(float(rw[oos].corr(rs[oos])), 3)},
        "legs": {"wheel": stats(rw), "sniper_honest": stats(rs)},
        "combos": {},
    }
    print(f"corr full={out['correlation']['full']} oos={out['correlation']['oos']}")
    print(f"wheel        : {stats(rw)}")
    print(f"sniper honest: {stats(rs)}")
    for wt in (0.3, 0.5, 0.7, 0.85, 1.0):
        r = wt * rw + (1 - wt) * rs
        full, o = stats(r), stats(r[oos])
        out["combos"][f"{int(wt*100)}/{int(100-wt*100)}"] = {"full": full, "oos": o}
        print(f"combo {int(wt*100):3d}/{int(100-wt*100):2d}: full={full} OOS={o}")
    pd.DataFrame({"wheel": wheel_eq.loc[idx],
                  "sniper_honest": sn_eq.loc[idx]}).to_csv(
        ROOT / "results/combo_wheel_sniper_v2_curves.csv")
    (ROOT / "results/combo_wheel_sniper_v2_research.json").write_text(
        json.dumps(out, indent=1))
    print("\nsaved → results/combo_wheel_sniper_v2_research.json + curves csv")


if __name__ == "__main__":
    main()
