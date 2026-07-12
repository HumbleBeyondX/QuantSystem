#!/usr/bin/env python3
"""Run the REFERENCE engine (scripts/backtest_btc_sniper.py) on ETH_USDT_4h.csv
with the validated combo params, and dump metrics + equity curve for comparison
against the NautilusTrader port.

Params (validated combo run): initial_capital=100_000, leverage=50,
atr_trail=3.0, max_hold=180, everything else class defaults.

Usage:
    .venv-nautilus/Scripts/python.exe scripts/nautilus_pilot/run_reference.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from backtest_btc_sniper import BTCSniperBacktest  # noqa: E402

DATA_FILE = PROJECT_ROOT / "data" / "sample" / "ETH_USDT_4h.csv"
OUT_FILE = Path(__file__).parent / "reference_results.json"


class NoCapBacktest(BTCSniperBacktest):
    """Reference engine with the 95%-of-margin 'liquidation' loss cap removed.

    Identical to BTCSniperBacktest._close except the net PnL is charged in
    full. Used to decompose how much of the reference performance comes from
    the loss-cap modeling artifact.
    """

    def _close(self, price, reason, timestamp):
        if abs(self._pos) < 1e-12:
            return
        pnl = (price - self._entry) * self._pos
        fee = abs(self._pos * price) * self.fee_rate
        net_pnl = pnl - fee  # NO liquidation cap

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


def load_df() -> pd.DataFrame:
    df = pd.read_csv(DATA_FILE)
    df.columns = [c.lower() for c in df.columns]
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.tz_localize(None)
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def daily_stats(equity_curve: list[dict]) -> dict:
    """Daily-resampled Sharpe / CAGR / MaxDD — same convention used for the
    Nautilus run so the two engines are compared apples-to-apples."""
    eq = pd.DataFrame(equity_curve)
    eq["ts"] = pd.to_datetime(eq["ts"])
    eq = eq.set_index("ts")["equity"]
    daily = eq.resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    total_ret = daily.iloc[-1] / daily.iloc[0] - 1
    n_days = (daily.index[-1] - daily.index[0]).days
    cagr = (1 + total_ret) ** (365 / max(n_days, 1)) - 1
    sharpe = float(rets.mean() / rets.std() * np.sqrt(365)) if rets.std() > 0 else 0.0
    cum = (1 + rets).cumprod()
    max_dd = float(((cum - cum.cummax()) / cum.cummax()).min())
    return {
        "final_equity": round(float(daily.iloc[-1]), 2),
        "total_return_pct": round(total_ret * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "sharpe_daily_365": round(sharpe, 3),
        "max_dd_pct": round(max_dd * 100, 2),
    }


def run_one(cls, df):
    bt = cls(
        initial_capital=100_000,
        leverage=50.0,
        atr_trail=3.0,
        max_hold=180,
    )
    return bt.run(df)


def main() -> None:
    df = load_df()
    print(f"Data: {df.shape[0]} bars | {df.timestamp.iloc[0]} -> {df.timestamp.iloc[-1]}")

    res = run_one(BTCSniperBacktest, df)
    m = res["metrics"]

    trades = res["trades"]
    opens = [t for t in trades if t["action"] in ("LONG", "SHORT")]
    n_short = sum(1 for t in opens if t["action"] == "SHORT")
    closed = [t for t in trades if t["action"].startswith("CLOSE")]

    ds = daily_stats(res["equity_curve"])

    print("\n=== REFERENCE ENGINE (native metrics, 4h-bar returns) ===")
    for k in ("total_return_pct", "annual_return_pct", "sharpe", "max_dd_pct",
              "win_rate", "profit_factor", "trades", "fees", "end_eq", "exits",
              "entry_types", "yearly"):
        print(f"  {k}: {m.get(k)}")
    print(f"  open trades: {len(opens)} (SHORT: {n_short})")

    print("\n=== REFERENCE ENGINE (daily-resampled, sqrt(365)) ===")
    for k, v in ds.items():
        print(f"  {k}: {v}")

    # ── Variant: liquidation loss cap removed ──
    res_nc = run_one(NoCapBacktest, df)
    m_nc = res_nc["metrics"]
    ds_nc = daily_stats(res_nc["equity_curve"])
    print("\n=== REFERENCE ENGINE, NO LOSS CAP (daily-resampled, sqrt(365)) ===")
    for k, v in ds_nc.items():
        print(f"  {k}: {v}")
    print(f"  trades: {m_nc.get('trades')} win_rate: {m_nc.get('win_rate')} "
          f"exits: {m_nc.get('exits')} entry_types: {m_nc.get('entry_types')}")

    out = {
        "params": {"initial_capital": 100_000, "leverage": 50, "atr_trail": 3.0,
                   "max_hold": 180, "defaults": "risk 0.05, stop 3.5 ATR, "
                   "activate 2.0 ATR, min_score 2, vol_min 1.2, DC10, cooldown 3, "
                   "fee 0.0005, pause 5/18"},
        "native_metrics": m,
        "daily_stats": ds,
        "n_open_trades": len(opens),
        "n_short": n_short,
        "n_closed": len(closed),
        "no_cap_native_metrics": m_nc,
        "no_cap_daily_stats": ds_nc,
    }
    OUT_FILE.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nSaved -> {OUT_FILE}")


if __name__ == "__main__":
    main()
