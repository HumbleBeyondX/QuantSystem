#!/usr/bin/env python3
"""Crypto wheel + regime switch, v2 — REAL IV, weekly regime, strict alignment.

Changes vs v1 (user directives 2026-07):
  1. REAL DATA: implied vol = Deribit DVOL index (real market 30d IV),
     fetched from the public API. No more HV×1.1 synthesis. Backtest window
     therefore starts at DVOL inception (2021-03 BTC / 2021-03 ETH).
  2. WEEKLY REGIME, DAILY EXECUTION: regime voted on COMPLETED weekly bars
     only (W-SUN). A trading day uses the label of the last week that closed
     strictly before it → structural debounce + zero look-ahead by design.
  3. ALIGNMENT AUDIT: price (yfinance UTC daily) and DVOL (Deribit UTC daily)
     inner-joined on date; dropped rows reported. Execution at day-d close
     uses only week-(d-1)-and-earlier signals and day-d market quotes.

Weekly regime votes (>=2 of 3, sign from 3w return):
  breakout   : weekly close > max/min of prior 8 weekly closes
  vol expand : std(weekly ret, 3w) / std(weekly ret, 12w) > 1.3
  burst      : |3w return| > 1.5 × std12w × sqrt(3)
"""
from __future__ import annotations
import json, sys, time, urllib.request, warnings
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
SHORT_NOTIONAL = 0.50
IS_END = "2023-12-31"


def fetch_dvol(currency: str) -> pd.Series:
    """Daily DVOL close (real 30d IV, %) from Deribit public API."""
    out = {}
    start = int(pd.Timestamp("2021-01-01").timestamp() * 1000)
    end_final = int(pd.Timestamp.now().timestamp() * 1000)
    while start < end_final:
        end = min(start + 900 * 86_400_000, end_final)  # ≤900 days/chunk
        url = ("https://www.deribit.com/api/v2/public/get_volatility_index_data"
               f"?currency={currency}&start_timestamp={start}"
               f"&end_timestamp={end}&resolution=1D")
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read())["result"]["data"]
        for ts, _o, _h, _l, c in data:
            out[pd.Timestamp(ts, unit="ms").normalize()] = float(c)
        start = end
        time.sleep(0.3)
    return pd.Series(out).sort_index()


def load_aligned(sym: str, cur: str) -> pd.DataFrame:
    px = yf.Ticker(sym).history(start="2020-06-01")["Close"]
    px.index = px.index.tz_localize(None).normalize()
    dvol = fetch_dvol(cur)
    df = pd.DataFrame({"px": px, "dvol": dvol})
    n_px, n_dv = df["px"].notna().sum(), df["dvol"].notna().sum()
    df = df.dropna()
    from data.quality.guards import ensure_bars_valid, flat_series
    df, rep = ensure_bars_valid(df, name=f"{sym}+dvol", stale_after_days=None)
    print(f"  DQ: {rep}")
    if flat_series(df["dvol"], window=10):
        raise ValueError(f"[{sym}] DVOL flat for 10 days — vendor data suspect")
    print(f"  [{sym}] alignment: {n_px} px days ∩ {n_dv} dvol days → "
          f"{len(df)} aligned ({df.index[0].date()} → {df.index[-1].date()})")
    return df


def weekly_regime_series(px: pd.Series) -> pd.Series:
    """Regime label per COMPLETED week (indexed by week-end Sunday)."""
    w = px.resample("W-SUN").last().dropna()
    ret = w.pct_change()
    labels = {}
    for i in range(13, len(w)):
        c = w.iloc[i]
        hi8, lo8 = w.iloc[i - 8:i].max(), w.iloc[i - 8:i].min()
        sd3 = ret.iloc[i - 2:i + 1].std()
        sd12 = ret.iloc[i - 11:i + 1].std()
        r3 = c / w.iloc[i - 3] - 1
        burst_th = 1.5 * sd12 * np.sqrt(3)
        vol_expand = int(sd3 / max(sd12, 1e-9) > 1.3)
        up = int(c > hi8) + vol_expand + int(r3 > burst_th)
        dn = int(c < lo8) + vol_expand + int(-r3 > burst_th)
        labels[w.index[i]] = ("trend_up" if up >= 2 and r3 > 0 else
                              "trend_dn" if dn >= 2 and r3 < 0 else "range")
    return pd.Series(labels)


def run(df: pd.DataFrame, allow_trend: bool = True, trend_entries: bool = True) -> dict:
    px, dvol = df["px"], df["dvol"]
    wr = weekly_regime_series(px)
    dates = px.index[px.index >= wr.index[0]]

    eq, curve = 100_000.0, {}
    strangle = trend = None
    n = wins = flips = 0
    regime_days = {"range": 0, "trend_up": 0, "trend_dn": 0}

    def reg_at(d):
        done = wr.index[wr.index < d]          # only weeks CLOSED before d
        return wr.loc[done[-1]] if len(done) else "range"

    for d in dates:
        s, iv = float(px.loc[d]), float(dvol.loc[d]) / 100.0
        reg = reg_at(d) if allow_trend else "range"
        regime_days[reg] += 1
        mtm = 0.0

        if strangle:
            dl = STRANGLE_DTE - (d - strangle["d0"]).days
            t = max(dl, 0.5) / 365
            v = (bs_put(s, strangle["kp"], t, R, iv)
                 + bs_call(s, strangle["kc"], t, R, iv))
            pnl = (strangle["credit"] - v * (1 + SPREAD)) * strangle["mult"]
            if reg != "range" or dl <= 0:
                eq += pnl; n += 1; wins += pnl > 0; strangle = None
                if reg != "range":
                    flips += 1
            else:
                mtm += pnl

        if trend:
            t = max(TREND_DTE - (d - trend["d0"]).days, 0.5) / 365
            f = bs_call if trend["kind"] == "up" else bs_put
            v = f(s, trend["k"], t, R, iv) * (1 - SPREAD)
            pnl = (v - trend["prem"]) * trend["mult"]
            frac = pnl / (trend["prem"] * trend["mult"])
            if reg == "range" or t <= 1 / 365 or frac <= -0.5:
                eq += pnl; n += 1; wins += pnl > 0; trend = None
            else:
                mtm += pnl

        if reg == "range" and strangle is None and trend is None:
            kp, kc = s * PUT_K, s * CALL_K
            credit = (bs_put(s, kp, STRANGLE_DTE / 365, R, iv)
                      + bs_call(s, kc, STRANGLE_DTE / 365, R, iv)) * (1 - SPREAD)
            if credit > 0:
                strangle = {"kp": kp, "kc": kc, "credit": credit,
                            "mult": eq * SHORT_NOTIONAL / s, "d0": d}
        elif (reg != "range" and trend is None and strangle is None
              and allow_trend and trend_entries):
            kind = "up" if reg == "trend_up" else "dn"
            f = bs_call if kind == "up" else bs_put
            prem = f(s, s, TREND_DTE / 365, R, iv) * (1 + SPREAD)
            if prem > 0:
                trend = {"k": s, "prem": prem,
                         "mult": eq * TREND_PREM / prem, "d0": d, "kind": kind}

        curve[d] = eq + mtm

    c = pd.Series(curve).sort_index()
    out = {}
    for lab, sl in [("FULL", c), ("IS", c[c.index <= IS_END]),
                    ("OOS", c[c.index > IS_END])]:
        r_d = sl.pct_change().dropna()
        if len(r_d) < 60:
            continue
        yrs = (sl.index[-1] - sl.index[0]).days / 365.25
        out[lab] = {"cagr": ((sl.iloc[-1] / sl.iloc[0]) ** (1 / yrs) - 1) * 100,
                    "sharpe": r_d.mean() / r_d.std() * np.sqrt(365) if r_d.std() > 0 else 0,
                    "dd": abs((sl / sl.cummax() - 1).min()) * 100}
    out.update(n=n, win=wins / max(n, 1) * 100, flips=flips,
               regime_days=regime_days, _c=c)
    return out


def main():
    curves = {}
    for sym, cur in [("BTC-USD", "BTC"), ("ETH-USD", "ETH")]:
        df = load_aligned(sym, cur)
        # weekly-regime sanity on known dates
        wr = weekly_regime_series(df["px"])
        checks = [("2021-11-07", "top area"), ("2022-06-19", "crash"),
                  ("2024-03-10", "bull"), ("2024-12-08", "bull")]
        print("  weekly regime spot-checks:",
              {ds: wr.loc[wr.index[wr.index <= ds][-1]] for ds, _ in checks
               if len(wr.index[wr.index <= ds])})
        for lab, kw in [("switch", dict(allow_trend=True)),
                        ("wheel-only", dict(allow_trend=False)),
                        ("wheel+PAUSE", dict(allow_trend=True, trend_entries=False))]:
            r = run(df, **kw)
            curves[(sym, lab)] = r["_c"]
            print(f"\n=== {sym} v2 REAL-IV — {lab} ===")
            for k in ("FULL", "IS", "OOS"):
                if k in r:
                    m = r[k]
                    print(f"  {k:5s} CAGR {m['cagr']:+7.1f}%  Sharpe {m['sharpe']:5.2f}  MaxDD {m['dd']:5.1f}%")
            print(f"  trades {r['n']}  win {r['win']:.0f}%  flips {r['flips']}  "
                  f"regime_days {r['regime_days']}")
            r["_c"].to_csv(ROOT / "results" / f"cryptowheel_v2_{sym[:3]}_{lab}.csv")

    print("\n=== 50/50 BTC+ETH switch combo ===")
    ra = curves[("BTC-USD", "switch")].pct_change()
    rb = curves[("ETH-USD", "switch")].pct_change()
    rc = ((ra + rb) / 2).dropna()
    for span, rr in [("FULL", rc), ("OOS", rc[rc.index > IS_END])]:
        sh = rr.mean() / rr.std() * np.sqrt(365) if rr.std() > 0 else 0
        cg = (1 + rr).prod() ** (365 / len(rr)) - 1
        eqc = (1 + rr).cumprod()
        dd = abs((eqc / eqc.cummax() - 1).min())
        print(f"  {span:4s} CAGR {cg*100:+7.1f}%  Sharpe {sh:5.2f}  DD {dd*100:4.1f}%")
    print("\nPricing: BS structure with REAL Deribit DVOL as IV input. "
          "Remaining approximations: 30d DVOL applied to 7d/21d tenors "
          "(term structure), no smile.")


if __name__ == "__main__":
    main()
