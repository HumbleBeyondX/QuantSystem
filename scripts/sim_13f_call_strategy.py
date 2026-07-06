#!/usr/bin/env python3
"""13F disclosure-day CALL strategy — portfolio-level simulated performance.

Rules (from the event study's surviving spec):
  Universe: conviction filers (berkshire/pershing/thirdpoint), weight >= 1%
  Entry:    first close after filing date, buy ATM call
  Exit:     5 trading days later (signal dies by day 10)
  Pricing:  Black-Scholes, IV = 63d realized vol * 1.15 (IV premium), floor 20%
  Costs:    3% of premium each way (single-name spreads) + $0.65/contract
  Sizing:   premium budget = 2% of current equity per event (compounding)

Variants: 30 DTE ATM (base), 14 DTE ATM (cheap theta), 30 DTE 5% OTM (lottery).
Honesty notes: no IV-crush/expansion modeled (symmetric assumption), synthetic
pricing not real chains — this bounds feasibility, it is not a tradable P&L.
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

CSV = ROOT / "results" / "pilot_13f_event_study.csv"
CONVICTION_FILERS = {"berkshire", "pershing", "thirdpoint"}
MIN_WEIGHT = 1.0
HOLD_DAYS = 5
SPREAD = 0.03
RISK_PER_EVENT = 0.02
START_EQUITY = 100_000.0
R = 0.04

_px_cache: dict[str, pd.Series] = {}
def prices(tk: str) -> pd.Series:
    if tk not in _px_cache:
        s = yf.Ticker(tk).history(start="2021-06-01")["Close"]
        s.index = s.index.tz_localize(None)
        _px_cache[tk] = s
    return _px_cache[tk]


def simulate(events: pd.DataFrame, dte: int, otm: float, label: str) -> dict:
    trades = []
    for _, e in events.iterrows():
        try:
            px = prices(e["ticker"])
        except Exception:
            continue
        fd = pd.Timestamp(e["filing_date"])
        after = px.index[px.index > fd]
        if len(after) < HOLD_DAYS + 1:
            continue
        t0, t1 = after[0], after[HOLD_DAYS]
        s0, s1 = float(px.loc[t0]), float(px.loc[t1])
        hist = px.loc[:t0].pct_change().dropna().tail(63)
        if len(hist) < 40:
            continue
        iv = max(float(hist.std() * np.sqrt(252)) * 1.15, 0.20)
        k = s0 * (1 + otm)
        c0 = bs_call(s0, k, dte / 365, R, iv)
        c1 = bs_call(s1, k, (dte - HOLD_DAYS - 2) / 365, R, iv)  # ~2 cal days per trade day extra
        if c0 <= 0.05:
            continue
        entry = c0 * (1 + SPREAD)
        exit_ = c1 * (1 - SPREAD)
        trades.append({
            "date": t0, "ticker": e["ticker"], "filer": e["filer"],
            "ret_premium": exit_ / entry - 1,
            "stock_move": s1 / s0 - 1,
        })
    tr = pd.DataFrame(trades).sort_values("date").reset_index(drop=True)
    if tr.empty:
        return {"label": label, "n": 0}

    # Portfolio: 2% premium budget per event, compounding
    equity = START_EQUITY
    curve = [(tr["date"].iloc[0], equity)]
    for _, t in tr.iterrows():
        pnl = equity * RISK_PER_EVENT * t["ret_premium"]
        equity += pnl
        curve.append((t["date"], equity))
    eq = pd.Series(dict(curve)).sort_index()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if years > 0 else 0
    dd = (eq / eq.cummax() - 1).min()
    # per-event Sharpe proxy: mean/std of per-event portfolio returns, ~20 events/yr
    ev_rets = tr["ret_premium"] * RISK_PER_EVENT
    n_per_year = len(tr) / years
    sharpe = ev_rets.mean() / ev_rets.std() * np.sqrt(n_per_year) if ev_rets.std() > 0 else 0
    return {
        "label": label, "n": len(tr), "years": round(years, 1),
        "total_ret%": round((eq.iloc[-1] / eq.iloc[0] - 1) * 100, 1),
        "cagr%": round(cagr * 100, 2), "maxDD%": round(abs(dd) * 100, 2),
        "sharpe~": round(sharpe, 2),
        "win%": round((tr["ret_premium"] > 0).mean() * 100, 0),
        "avg_prem_ret%": round(tr["ret_premium"].mean() * 100, 1),
        "med_prem_ret%": round(tr["ret_premium"].median() * 100, 1),
        "best%": round(tr["ret_premium"].max() * 100, 0),
        "worst%": round(tr["ret_premium"].min() * 100, 0),
        "_trades": tr,
    }


def main():
    df = pd.read_csv(CSV)
    conv = df[df["filer"].isin(CONVICTION_FILERS) & (df["pct_port"] >= MIN_WEIGHT)]
    print(f"Conviction events: {len(conv)} (filers={sorted(conv['filer'].unique())})")

    results = [
        simulate(conv, 30, 0.00, "30DTE ATM (base)"),
        simulate(conv, 14, 0.00, "14DTE ATM (low theta)"),
        simulate(conv, 30, 0.05, "30DTE 5% OTM (lottery)"),
        simulate(df[df["pct_port"] >= 1.0], 30, 0.00, "ALL filers w>=1% (control)"),
    ]
    print("\n" + "=" * 110)
    print("13F CALL STRATEGY — simulated portfolio performance (2% premium/event, compounding)")
    print("=" * 110)
    cols = ["label", "n", "years", "total_ret%", "cagr%", "maxDD%", "sharpe~",
            "win%", "avg_prem_ret%", "med_prem_ret%", "best%", "worst%"]
    print(pd.DataFrame([{k: r.get(k) for k in cols} for r in results]).to_string(index=False))

    base = results[0]
    if base.get("n"):
        tr = base["_trades"]
        print("\nBase variant — every trade:")
        for _, t in tr.iterrows():
            print(f"  {str(t['date'].date())} {t['filer'][:9]:9s} {t['ticker']:6s} "
                  f"stock {t['stock_move']*100:+6.2f}%  → call {t['ret_premium']*100:+7.1f}%")
        tr.drop(columns=["_trades"], errors="ignore").to_csv(
            ROOT / "results" / "sim_13f_call_trades.csv", index=False)
        print("\nSaved → results/sim_13f_call_trades.csv")


if __name__ == "__main__":
    main()
