#!/usr/bin/env python3
"""Sector-rotation → leader stocks → high-leverage calls.

Spec (user, 2026-07-09): track style/sector rotation, buy calls on leaders
of the rotating-in sectors. Gate: Sharpe >= 1.5 OOS. High DD acceptable.
Trade frequency must not be too low.

Design:
  Rotation signal: sector ETF relative strength vs SPY, blended 21d+63d,
                   recomputed every REBAL_DAYS trading days → top-K sectors.
  Leaders:         2 mega-cap leaders per sector (static map — survivorship
                   caveat noted in output).
  Instrument:      30-DTE ATM calls, BS-priced (IV = 63d HV × 1.15, floor 25%).
                   No historical chains available — synthetic pricing is a
                   feasibility bound, flagged in output.
  Hold:            to next rebalance (~REBAL_DAYS), or -50% premium stop.
  Sizing:          PREMIUM_PCT of equity per position, up to 2K positions.
  MTM:             positions repriced DAILY → honest vol/Sharpe/DD.
  Costs:           3% premium spread each way.
  Validation:      IS 2019-2023 / OOS 2024-2026 + full-period stats.
  Variants:        calls vs stock (same signal, 1x) vs SPY B&H.
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
from strategies.options.gamma_scalp_strategy import bs_call

SECTOR_LEADERS = {
    "XLK": ["NVDA", "MSFT"], "XLF": ["JPM", "GS"],   "XLV": ["LLY", "UNH"],
    "XLY": ["AMZN", "TSLA"], "XLC": ["META", "GOOGL"], "XLE": ["XOM", "CVX"],
    "XLI": ["GE", "CAT"],    "XLB": ["LIN", "FCX"],  "XLU": ["NEE", "VST"],
    "XLP": ["COST", "WMT"],  "XLRE": ["PLD", "AMT"],
}
SECTORS = list(SECTOR_LEADERS)
TOP_K = 2
REBAL_DAYS = 10           # ~biweekly → decent frequency
DTE = 30
PREMIUM_PCT = 0.03        # 3% equity per call position
STOP_PREMIUM = -0.50      # cut at -50% of premium
SPREAD = 0.03
R = 0.04
START = "2018-06-01"      # warmup before 2019
IS_END = "2023-12-31"


def fetch_all():
    tickers = SECTORS + ["SPY"] + sorted({t for v in SECTOR_LEADERS.values() for t in v})
    px = {}
    for t in tickers:
        s = yf.Ticker(t).history(start=START, auto_adjust=True)["Close"]
        s.index = s.index.tz_localize(None).normalize()
        if len(s) > 200:
            px[t] = s
    df = pd.DataFrame(px).ffill()
    return df.dropna(subset=["SPY"])


def run(px: pd.DataFrame, instrument: str, strike_mult: float = 1.0,
        dte: int = DTE, prem_pct: float = PREMIUM_PCT, stop: float = STOP_PREMIUM,
        trend_gate: bool = False, pick_by_momentum: bool = False) -> dict:
    """instrument: 'calls' or 'stock'. strike_mult<1 → ITM calls."""
    dates = px.index[px.index >= "2019-01-01"]
    equity = 100_000.0
    curve = {}
    open_pos = []           # dicts: ticker, entry_spot, strike, iv, entry_px, contracts_$, entry_date, dte_left
    n_trades = 0
    wins = 0
    rets = []

    rebal_marks = set(dates[::REBAL_DAYS])
    for d in dates:
        spot_row = px.loc[d]

        # ── daily MTM + stops ──
        still = []
        for p in open_pos:
            s = spot_row.get(p["ticker"], np.nan)
            if np.isnan(s):
                still.append(p); continue
            days_held = (d - p["entry_date"]).days
            t_left = max(dte - days_held, 1) / 365
            if instrument == "calls":
                mark = bs_call(float(s), p["strike"], t_left, R, p["iv"])
                pnl_frac = (mark * (1 - SPREAD)) / p["entry_px"] - 1
            else:
                pnl_frac = float(s) / p["entry_spot"] - 1
            p["pnl_frac"] = pnl_frac
            # stop (calls only) or expiry-ish
            if instrument == "calls" and pnl_frac <= stop:
                equity += p["alloc"] * pnl_frac
                rets.append(pnl_frac); n_trades += 1; wins += pnl_frac > 0
            else:
                still.append(p)
        open_pos = still

        # ── rebalance ──
        if d in rebal_marks:
            # close all
            for p in open_pos:
                equity += p["alloc"] * p.get("pnl_frac", 0.0)
                rets.append(p.get("pnl_frac", 0.0)); n_trades += 1
                wins += p.get("pnl_frac", 0.0) > 0
            open_pos = []
            # rank sectors by blended relative strength
            hist = px.loc[:d]
            if len(hist) < 70:
                continue
            rs = {}
            for sec in SECTORS:
                if sec not in hist or np.isnan(hist[sec].iloc[-1]):
                    continue
                r21 = hist[sec].iloc[-1] / hist[sec].iloc[-21] - hist["SPY"].iloc[-1] / hist["SPY"].iloc[-21]
                r63 = hist[sec].iloc[-1] / hist[sec].iloc[-63] - hist["SPY"].iloc[-1] / hist["SPY"].iloc[-63]
                rs[sec] = 0.5 * r21 + 0.5 * r63
            top = sorted(rs, key=rs.get, reverse=True)[:TOP_K]
            # trend gate: no new entries in SPY downtrend
            if trend_gate:
                spy_h = hist["SPY"].dropna()
                if len(spy_h) >= 200 and spy_h.iloc[-1] < spy_h.rolling(200).mean().iloc[-1]:
                    curve[d] = equity + sum(p_["alloc"] * p_.get("pnl_frac", 0.0) for p_ in open_pos)
                    continue
            # only enter when the sector actually beats SPY (positive RS)
            for sec in top:
                if rs[sec] <= 0:
                    continue
                cands = SECTOR_LEADERS[sec]
                if pick_by_momentum:
                    mom = {}
                    for tk in cands:
                        col = hist.get(tk)
                        if col is not None and len(col.dropna()) > 70:
                            mom[tk] = col.iloc[-1] / col.iloc[-63] - 1
                    cands = sorted(mom, key=mom.get, reverse=True)[:1] if mom else []
                for tk in cands:
                    s = spot_row.get(tk, np.nan)
                    if np.isnan(s):
                        continue
                    logret = np.log(px[tk].loc[:d]).diff().dropna().tail(63)
                    if len(logret) < 40:
                        continue
                    iv = max(float(logret.std() * np.sqrt(252)) * 1.15, 0.25)
                    alloc = equity * prem_pct
                    if instrument == "calls":
                        k = float(s) * strike_mult
                        c0 = bs_call(float(s), k, dte / 365, R, iv)
                        if c0 <= 0.05:
                            continue
                        open_pos.append({"ticker": tk, "entry_spot": float(s),
                                         "strike": k, "iv": iv,
                                         "entry_px": c0 * (1 + SPREAD),
                                         "alloc": alloc, "entry_date": d})
                    else:
                        open_pos.append({"ticker": tk, "entry_spot": float(s),
                                         "strike": 0, "iv": 0, "entry_px": float(s),
                                         "alloc": alloc, "entry_date": d})

        # mark equity daily (open positions at current pnl_frac)
        open_mtm = sum(p["alloc"] * p.get("pnl_frac", 0.0) for p in open_pos)
        curve[d] = equity + open_mtm

    eq = pd.Series(curve).sort_index()
    out = {}
    for label, sl in [("FULL", eq),
                      ("IS 2019-2023", eq[eq.index <= IS_END]),
                      ("OOS 2024-2026", eq[eq.index > IS_END])]:
        r_d = sl.pct_change().dropna()
        if len(r_d) < 60:
            continue
        years = (sl.index[-1] - sl.index[0]).days / 365.25
        cagr = (sl.iloc[-1] / sl.iloc[0]) ** (1 / years) - 1
        sharpe = r_d.mean() / r_d.std() * np.sqrt(252) if r_d.std() > 0 else 0
        dd = (sl / sl.cummax() - 1).min()
        out[label] = {"cagr%": round(cagr * 100, 1), "sharpe": round(sharpe, 2),
                      "maxDD%": round(abs(dd) * 100, 1)}
    out["n_trades"] = n_trades
    out["trades_per_year"] = round(n_trades / ((eq.index[-1] - eq.index[0]).days / 365.25), 0)
    out["win%"] = round(wins / max(n_trades, 1) * 100, 0)
    out["_curve"] = eq
    return out


def main():
    print("Fetching prices (33 tickers)...")
    px = fetch_all()
    print(f"  {px.shape[0]} days × {px.shape[1]} tickers, → {px.index[-1].date()}")

    variants = [
        ("v1 ATM calls", dict(instrument="calls")),
        ("v1 stock", dict(instrument="stock")),
        ("v2 ITM calls (0.9K 60DTE trend+mom)", dict(
            instrument="calls", strike_mult=0.90, dte=60, prem_pct=0.08,
            stop=-0.35, trend_gate=True, pick_by_momentum=True)),
        ("v2 stock (trend+mom)", dict(
            instrument="stock", prem_pct=0.15,
            trend_gate=True, pick_by_momentum=True)),
    ]
    for label, kw in variants:
        res = run(px, **kw)
        print(f"\n=== {label} ===")
        for k in ["FULL", "IS 2019-2023", "OOS 2024-2026"]:
            if k in res:
                m = res[k]
                print(f"  {k:14s} CAGR {m['cagr%']:+6.1f}%  Sharpe {m['sharpe']:5.2f}  MaxDD {m['maxDD%']:5.1f}%")
        print(f"  trades: {res['n_trades']} total (~{res['trades_per_year']:.0f}/yr), win {res['win%']:.0f}%")
        slug = label.replace(" ", "_").replace("(", "").replace(")", "").replace("+", "")
        res["_curve"].to_csv(ROOT / "results" / f"rotation_leader_{slug}.csv")

    spy = px["SPY"][px.index >= "2019-01-01"]
    r_d = spy.pct_change().dropna()
    print(f"\n  SPY B&H reference: Sharpe {r_d.mean()/r_d.std()*np.sqrt(252):.2f}")
    print("\nCAVEATS: synthetic BS pricing (no real chains); static leader map "
          "has survivorship bias; IV=HV×1.15 ignores vol smile & event crush.")


if __name__ == "__main__":
    main()
