#!/usr/bin/env python3
"""Portfolio combo: BTC wheel-only (real-IV, chain-calibrated) + ETH Sniper.

Wheel leg pricing = real Deribit DVOL history × REAL live-chain calibration
(data/altdata/deribit_calibration.json, measured 2026-07):
  put leg IV  = DVOL × tenor(7/30) × put_smile   (0.964 × 1.299)
  call leg IV = DVOL × tenor(7/30) × call_smile  (0.964 × 0.949)
Disclosed approximation: single-day smile applied to all history (smile
steepens in fear → put credits UNDERstated in crises → conservative).

Sniper leg = existing BTCSniperBacktest engine on ETH 4h with the live
ETH-tuned params (trail 3.0, hold 180, 50x), real OHLCV.

Output: per-leg stats, daily-return correlation, combo Sharpe across weights,
IS(≤2023)/OOS(2024+) split. Gate: OOS ≥ 1.5.
"""
from __future__ import annotations
import json, sys, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd
from data.quality.guards import ensure_utc_naive, to_utc_naive
from strategies.options.gamma_scalp_strategy import bs_call, bs_put
from scripts.backtest_crypto_wheel_v2 import load_aligned

sys.path.insert(0, str(ROOT / "scripts"))
from backtest_btc_sniper import BTCSniperBacktest  # noqa: E402

R, SPREAD = 0.03, 0.03
PUT_K, CALL_K, DTE = 0.92, 1.08, 7
SHORT_NOTIONAL = 0.50
IS_END = "2023-12-31"
CAL = json.load(open(ROOT / "data/altdata/deribit_calibration.json"))
PUT_IV_MULT = CAL["tenor_7_30"] * CAL["put_smile"]
CALL_IV_MULT = CAL["tenor_7_30"] * CAL["call_smile"]


def wheel_curve(df: pd.DataFrame) -> pd.Series:
    px, dvol = df["px"], df["dvol"]
    eq, curve, pos = 100_000.0, {}, None
    for d in px.index:
        s = float(px.loc[d])
        ivp = float(dvol.loc[d]) / 100 * PUT_IV_MULT
        ivc = float(dvol.loc[d]) / 100 * CALL_IV_MULT
        mtm = 0.0
        if pos:
            dl = DTE - (d - pos["d0"]).days
            t = max(dl, 0.5) / 365
            v = bs_put(s, pos["kp"], t, R, ivp) + bs_call(s, pos["kc"], t, R, ivc)
            pnl = (pos["credit"] - v * (1 + SPREAD)) * pos["mult"]
            if dl <= 0:
                eq += pnl; pos = None
            else:
                mtm = pnl
        if pos is None:
            kp, kc = s * PUT_K, s * CALL_K
            credit = (bs_put(s, kp, DTE / 365, R, ivp)
                      + bs_call(s, kc, DTE / 365, R, ivc)) * (1 - SPREAD)
            if credit > 0:
                pos = {"kp": kp, "kc": kc, "credit": credit,
                       "mult": eq * SHORT_NOTIONAL / s, "d0": d}
        curve[d] = eq + mtm
    return pd.Series(curve).sort_index()


def sniper_curve() -> pd.Series:
    df = pd.read_csv(ROOT / "data/sample/ETH_USDT_4h.csv")
    df.columns = [c.lower() for c in df.columns]
    df["timestamp"] = to_utc_naive(pd.DatetimeIndex(pd.to_datetime(df["timestamp"], utc=True)))
    df = df.sort_values("timestamp").reset_index(drop=True)
    bt = BTCSniperBacktest(initial_capital=100_000, leverage=50,
                           atr_trail=3.0, max_hold=180)
    bt.run(df)
    ec = pd.DataFrame(bt._equity_curve)
    ec["ts"] = pd.to_datetime(ec["ts"])
    return ec.set_index("ts")["equity"].resample("D").last().dropna()


def stats(r: pd.Series, label: str) -> dict:
    yrs = (r.index[-1] - r.index[0]).days / 365.25
    eqc = (1 + r).cumprod()
    return {"label": label,
            "cagr%": round((eqc.iloc[-1] ** (1 / yrs) - 1) * 100, 1),
            "sharpe": round(r.mean() / r.std() * np.sqrt(365), 2) if r.std() > 0 else 0,
            "maxDD%": round(abs((eqc / eqc.cummax() - 1).min()) * 100, 1)}


def main():
    print(f"Calibration (real chain {CAL['measured'][:10]}): "
          f"put IV ×{PUT_IV_MULT:.3f}, call IV ×{CALL_IV_MULT:.3f} on DVOL")
    btc = load_aligned("BTC-USD", "BTC")
    w = wheel_curve(btc)
    s = sniper_curve()

    idx = w.index.intersection(s.index)
    rw = w.loc[idx].pct_change().dropna()
    rs = s.loc[idx].pct_change().dropna()
    idx2 = rw.index.intersection(rs.index)
    rw, rs = rw.loc[idx2], rs.loc[idx2]
    print(f"\naligned window: {idx2[0].date()} → {idx2[-1].date()} ({len(idx2)} days)")
    print(f"daily-return correlation: {rw.corr(rs):+.3f}")

    rows = [stats(rw, "BTC wheel (calibrated)"), stats(rs, "ETH Sniper")]
    for wt in (0.3, 0.5, 0.7):
        rows.append(stats(wt * rw + (1 - wt) * rs, f"combo {int(wt*100)}/{int((1-wt)*100)}"))
    print("\n=== FULL window ===")
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n=== IS / OOS (split 2023-12-31) ===")
    for span, mask in [("IS ", idx2 <= IS_END), ("OOS", idx2 > IS_END)]:
        row = []
        for lab, r in [("wheel", rw[mask]), ("sniper", rs[mask]),
                       ("combo50", (0.5 * rw + 0.5 * rs)[mask])]:
            st = stats(r, lab)
            row.append(f"{lab} Sharpe {st['sharpe']:5.2f} CAGR {st['cagr%']:+6.1f}% DD {st['maxDD%']:4.1f}%")
        print(f"  {span}: " + " | ".join(row))

    pd.DataFrame({"wheel": w.loc[idx], "sniper": s.loc[idx]}).to_csv(
        ROOT / "results" / "combo_wheel_sniper_curves.csv")
    print("\nSaved → results/combo_wheel_sniper_curves.csv")


if __name__ == "__main__":
    main()
