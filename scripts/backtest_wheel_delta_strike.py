#!/usr/bin/env python3
"""BTC wheel — delta-targeted strike variant (vol-adaptive distance).

The fixed 0.92/1.08 wheel prices strikes at a constant % of spot: too
close in high-DVOL weeks (0.89 sigma), too far in calm ones (1.44 sigma).
This variant solves strikes for a target Black-Scholes delta each week,
so the statistical distance is constant across vol regimes.

Everything else is IDENTICAL to the validated wheel_curve machinery
(real DVOL x live-chain calibrated IV, 7-DTE weekly cycle, 50% notional,
3% premium spread, hold to expiry).

Protocol: IS-only selection (to 2024-08-31), OOS blind except top-5;
winner gets the full validation suite (fee stress / WF / LOO).
Baseline to beat: fixed-strike wheel base Sharpe 1.34 (all suite checks
passed), gate = 1.5.
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import scripts.backtest_combo_wheel_sniper as combo  # noqa: E402
from scripts.backtest_crypto_wheel_v2 import load_aligned  # noqa: E402
from strategies.options.gamma_scalp_strategy import bs_call, bs_put  # noqa: E402

R, SPREAD, DTE, SHORT_NOTIONAL = combo.R, combo.SPREAD, combo.DTE, combo.SHORT_NOTIONAL
PUT_IV_MULT, CALL_IV_MULT = combo.PUT_IV_MULT, combo.CALL_IV_MULT
OOS_START = "2024-09-01"


def strike_for_delta(s: float, t: float, iv: float, target: float,
                     kind: str) -> float:
    """K such that |BS delta| == target (put: N(d1)-1=-target; call: N(d1)=target)."""
    d1 = norm.ppf(1 - target) if kind == "put" else norm.ppf(target)
    return float(s * np.exp((R + 0.5 * iv * iv) * t - d1 * iv * np.sqrt(t)))


def wheel_delta_curve(df: pd.DataFrame, put_delta: float, call_delta: float,
                      spread_mult: float = 1.0) -> pd.Series:
    px, dvol = df["px"], df["dvol"]
    spread = SPREAD * spread_mult
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
            pnl = (pos["credit"] - v * (1 + spread)) * pos["mult"]
            if dl <= 0:
                eq += pnl
                pos = None
            else:
                mtm = pnl
        if pos is None:
            t0 = DTE / 365
            kp = strike_for_delta(s, t0, ivp, put_delta, "put")
            kc = strike_for_delta(s, t0, ivc, call_delta, "call")
            credit = (bs_put(s, kp, t0, R, ivp)
                      + bs_call(s, kc, t0, R, ivc)) * (1 - spread)
            if credit > 0:
                pos = {"kp": kp, "kc": kc, "credit": credit,
                       "mult": eq * SHORT_NOTIONAL / s, "d0": d}
        curve[d] = eq + mtm
    return pd.Series(curve).sort_index()


def st(r: pd.Series) -> dict:
    r = r.dropna()
    if len(r) < 60 or r.std() == 0:
        return {}
    e = (1 + r).cumprod()
    return {"sharpe": round(float(r.mean() / r.std() * np.sqrt(365)), 2),
            "max_dd": round(float((e / e.cummax() - 1).min()), 3),
            "cagr": round(float(e.iloc[-1] ** (365 / len(r)) - 1), 3)}


def main() -> None:
    df = load_aligned("BTC-USD", "BTC")
    deltas = [0.10, 0.15, 0.20, 0.25, 0.30]
    results = []
    print(f"sweeping {len(deltas)**2} delta pairs (IS-only selection, OOS from {OOS_START})")
    for pd_, cd_ in itertools.product(deltas, deltas):
        eq = wheel_delta_curve(df, pd_, cd_)
        r = eq.pct_change().dropna()
        is_mask = r.index < OOS_START
        results.append({"put_delta": pd_, "call_delta": cd_,
                        "is": st(r[is_mask]), "oos": st(r[~is_mask]),
                        "full": st(r)})
    ok = [x for x in results if x["is"].get("sharpe") is not None]
    ok.sort(key=lambda x: x["is"]["sharpe"], reverse=True)
    print("\n=== TOP 10 by IS Sharpe (OOS revealed for top 5 only) ===")
    for i, x in enumerate(ok[:10]):
        line = (f"#{i+1} putΔ={x['put_delta']:.2f} callΔ={x['call_delta']:.2f} "
                f"| IS {x['is']}")
        if i < 5:
            line += f" | OOS {x['oos']} | full {x['full']}"
        print(line)
    # fixed-strike baseline on the same data/window for honest comparison
    base_eq = combo.wheel_curve(df)
    br = base_eq.pct_change().dropna()
    print(f"\nbaseline fixed 0.92/1.08: IS {st(br[br.index < OOS_START])} "
          f"OOS {st(br[br.index >= OOS_START])} full {st(br)}")
    (ROOT / "results/wheel_delta_strike_sweep.json").write_text(
        json.dumps({"protocol": "IS-only selection", "oos_start": OOS_START,
                    "baseline_fixed": {"is": st(br[br.index < OOS_START]),
                                       "oos": st(br[br.index >= OOS_START]),
                                       "full": st(br)},
                    "results": ok}, indent=1))
    print("saved → results/wheel_delta_strike_sweep.json")


if __name__ == "__main__":
    main()
