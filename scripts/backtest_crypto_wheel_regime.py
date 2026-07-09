#!/usr/bin/env python3
"""BTC/ETH regime-switching options: wheel premium in chop, long calls in trend.

RANGE mode  (default):  sell weekly 7-DTE strangle (put 0.92S / call 1.08S),
                        cash-secured sizing, roll weekly, hard-close on flip.
TREND mode  (breakout): close shorts immediately; buy 21-DTE ATM calls
                        (uptrend) / puts (downtrend), premium 10% equity,
                        trail out when regime decays.

Regime factors (daily, composite vote >= 2 of 3 → TREND):
  1. Donchian breakout: close > 20d high (up) / < 20d low (down)
  2. Vol expansion: RV5 / RV20 > 1.4
  3. Momentum burst: |7d return| > 1.5 × RV20_weekly

Pricing: BS with IV = HV20 × 1.10 (crypto VRP conservative), spread 3%/leg.
Gate: OOS Sharpe >= 1.5. IS 2019-2023 / OOS 2024-2026.
"""
from __future__ import annotations
import sys, warnings
from pathlib import Path
warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd
import yfinance as yf
from strategies.options.gamma_scalp_strategy import bs_call, bs_put

R, SPREAD = 0.03, 0.03
PUT_K, CALL_K, STRANGLE_DTE = 0.92, 1.08, 7
TREND_DTE, TREND_PREM = 21, 0.10
SHORT_NOTIONAL = 0.50          # strangle notional = 50% equity per side
IS_END = "2023-12-31"


def regime(px: pd.Series, d) -> str:
    h = px.loc[:d]
    if len(h) < 40:
        return "range"
    c = h.iloc[-1]
    hi20, lo20 = h.iloc[-21:-1].max(), h.iloc[-21:-1].min()
    lr = np.log(h).diff().dropna()
    rv5 = lr.tail(5).std() * np.sqrt(365)
    rv20 = lr.tail(20).std() * np.sqrt(365)
    r7 = c / h.iloc[-8] - 1
    # int() each vote: comparisons here yield np.bool_, whose `+` is a
    # LOGICAL OR (saturates at 1) — summing them raw can never reach 2.
    vol_expand = int(rv5 / max(rv20, 1e-9) > 1.4)
    burst = 1.5 * rv20 / np.sqrt(52)
    votes_up = int(c > hi20) + vol_expand + int(r7 > burst)
    votes_dn = int(c < lo20) + vol_expand + int(-r7 > burst)
    if votes_up >= 2 and r7 > 0:
        return "trend_up"
    if votes_dn >= 2 and r7 < 0:
        return "trend_dn"
    return "range"


def run(px: pd.Series, allow_trend: bool = True, trend_prem: float = TREND_PREM,
        trail: bool = False) -> dict:
    dates = px.index[px.index >= "2019-01-01"]
    eq, curve = 100_000.0, {}
    strangle = None   # dict: kp,kc,iv,credit,mult,d0
    trend = None      # dict: k,iv,prem,mult,d0,kind
    n = wins = flips = 0

    def iv_at(d):
        lr = np.log(px.loc[:d]).diff().dropna().tail(20)
        return max(float(lr.std() * np.sqrt(365)) * 1.10, 0.35)

    def strangle_val(s, p, days_left):
        t = max(days_left, 0.5) / 365
        return bs_put(s, p["kp"], t, R, p["iv"]) + bs_call(s, p["kc"], t, R, p["iv"])

    for d in dates:
        s = float(px.loc[d])
        reg = regime(px, d) if allow_trend else "range"
        mtm = 0.0

        # ── manage strangle ──
        if strangle:
            dl = STRANGLE_DTE - (d - strangle["d0"]).days
            v = strangle_val(s, strangle, dl)
            pnl = (strangle["credit"] - v * (1 + SPREAD)) * strangle["mult"]
            if reg != "range" or dl <= 0:
                eq += pnl; n += 1; wins += pnl > 0; strangle = None
                if reg != "range":
                    flips += 1
            else:
                mtm += pnl

        # ── manage trend position ──
        if trend:
            t = max(TREND_DTE - (d - trend["d0"]).days, 0.5) / 365
            f = bs_call if trend["kind"] == "up" else bs_put
            v = f(s, trend["k"], t, R, trend["iv"]) * (1 - SPREAD)
            pnl = (v - trend["prem"]) * trend["mult"]
            frac = pnl / (trend["prem"] * trend["mult"])
            trend["peak"] = max(trend.get("peak", 0.0), frac)
            trail_hit = trail and trend["peak"] > 0.5 and frac < trend["peak"] - 0.4
            if reg == "range" or t <= 1 / 365 or frac <= -0.5 or trail_hit:
                eq += pnl; n += 1; wins += pnl > 0; trend = None
            else:
                mtm += pnl

        # ── entries ──
        if reg == "range" and strangle is None and trend is None:
            iv = iv_at(d)
            kp, kc = s * PUT_K, s * CALL_K
            credit = (bs_put(s, kp, STRANGLE_DTE / 365, R, iv)
                      + bs_call(s, kc, STRANGLE_DTE / 365, R, iv)) * (1 - SPREAD)
            if credit > 0:
                strangle = {"kp": kp, "kc": kc, "iv": iv, "credit": credit,
                            "mult": eq * SHORT_NOTIONAL / s, "d0": d}
        elif reg != "range" and trend is None and strangle is None and allow_trend:
            iv = iv_at(d)
            kind = "up" if reg == "trend_up" else "dn"
            f = bs_call if kind == "up" else bs_put
            prem = f(s, s, TREND_DTE / 365, R, iv) * (1 + SPREAD)
            if prem > 0:
                trend = {"k": s, "iv": iv, "prem": prem,
                         "mult": eq * trend_prem / prem, "d0": d, "kind": kind}

        curve[d] = eq + mtm

    c = pd.Series(curve).sort_index()
    out = {}
    for lab, sl in [("FULL", c), ("IS", c[c.index <= IS_END]), ("OOS", c[c.index > IS_END])]:
        r_d = sl.pct_change().dropna()
        if len(r_d) < 60:
            continue
        yrs = (sl.index[-1] - sl.index[0]).days / 365.25
        out[lab] = {"cagr": ((sl.iloc[-1] / sl.iloc[0]) ** (1 / yrs) - 1) * 100,
                    "sharpe": r_d.mean() / r_d.std() * np.sqrt(365) if r_d.std() > 0 else 0,
                    "dd": abs((sl / sl.cummax() - 1).min()) * 100}
    out.update(n=n, win=wins / max(n, 1) * 100, flips=flips, _c=c)
    return out


def main():
    curves = {}
    for sym in ["BTC-USD", "ETH-USD"]:
        px = yf.Ticker(sym).history(start="2018-01-01")["Close"]
        px.index = px.index.tz_localize(None).normalize()
        grid = [("base p=0.10", dict()),
                ("p=0.15 trail", dict(trend_prem=0.15, trail=True)),
                ("p=0.20 trail", dict(trend_prem=0.20, trail=True)),
                ("p=0.10 trail", dict(trail=True))]
        for lab, kw in grid:
            r = run(px, True, **kw)
            curves[(sym, lab)] = r["_c"]
            print(f"\n=== {sym} — {lab} ===")
            for k in ("FULL", "IS", "OOS"):
                if k in r:
                    m = r[k]
                    print(f"  {k:5s} CAGR {m['cagr']:+7.1f}%  Sharpe {m['sharpe']:5.2f}  MaxDD {m['dd']:5.1f}%")
            print(f"  trades {r['n']}  win {r['win']:.0f}%  regime-flips {r['flips']}")

    print("\nCAVEAT: BS synthetic pricing, IV=HV20x1.10 (Deribit VRP usually richer "
          "-> understates seller edge); no funding/liquidity modeling.")


if __name__ == "__main__":
    main()
