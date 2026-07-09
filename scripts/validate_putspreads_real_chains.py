#!/usr/bin/env python3
"""OOS validation of leader put-spreads with REAL Alpaca option chains.

Same signal as backtest_leader_putspreads.py (trend gate + momentum top-4,
10-day rebal, sell 0.95K / buy 0.85K ~30DTE). Pricing swapped from BS
synthetic to REAL historical option daily bars (Alpaca, available since
2024-02) over the OOS window — exactly the segment where the synthetic sim
scored Sharpe 0.97 vs the 1.5 gate.

Real-chain mechanics:
  * Expiry: Friday nearest entry+30d (probe ±10d window).
  * Strikes: ladder-probe rounded strikes (1/2.5/5/10 increments) nearest
    target until BOTH legs have a bar on entry day.
  * Entry credit = short_close − long_close, haircut 3% (cross the spread).
  * Daily MTM from real bar closes (ffill gaps); exit at +50% credit /
    −200% credit / DTE−2 / 10-day rebal — identical rules.
  * Coverage stats reported: % trades filled with real data, % real marks.
"""
from __future__ import annotations
import sys, time, warnings
from datetime import timedelta
from pathlib import Path
warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import yfinance as yf
from data.quality.guards import ensure_utc_naive, to_utc_naive
from dotenv import load_dotenv
load_dotenv()

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionBarsRequest
from alpaca.data.timeframe import TimeFrame
import os

LEADERS = ["NVDA", "MSFT", "META", "GOOGL", "AMZN", "TSLA", "JPM", "LLY",
           "XOM", "CAT", "COST", "GE"]
START = "2024-02-05"
DTE, SHORT_K, LONG_K = 30, 0.95, 0.85
REBAL, TP, SL = 10, 0.50, -2.00
RISK_PCT, MAX_POS, HAIRCUT = 0.05, 4, 0.03

_client = OptionHistoricalDataClient(os.environ["ALPACA_API_KEY"],
                                     os.environ["ALPACA_SECRET_KEY"])
_bar_cache: dict = {}


def occ(root: str, expiry, strike: float) -> str:
    return f"{root}{expiry:%y%m%d}P{int(round(strike * 1000)):08d}"


def get_bars(symbols, start, end):
    """Daily close series per OCC symbol, cached."""
    need = [s for s in symbols if (s, start.date(), end.date()) not in _bar_cache]
    if need:
        try:
            resp = _client.get_option_bars(OptionBarsRequest(
                symbol_or_symbols=need, timeframe=TimeFrame.Day,
                start=start, end=end))
            data = resp.data if hasattr(resp, "data") else {}
        except Exception:
            data = {}
        time.sleep(0.3)
        for s in need:
            bars = data.get(s, [])
            ser = pd.Series({ensure_utc_naive(pd.DatetimeIndex([b.timestamp]))[0]:
                             float(b.close) for b in bars}).sort_index()
            _bar_cache[(s, start.date(), end.date())] = ser
    return {s: _bar_cache[(s, start.date(), end.date())] for s in symbols}


def candidate_strikes(target: float):
    out = []
    for inc in (1, 2.5, 5, 10):
        for k in (round(target / inc) * inc, np.floor(target / inc) * inc,
                  np.ceil(target / inc) * inc):
            if k > 0 and k not in out:
                out.append(round(float(k), 2))
    return sorted(out, key=lambda k: abs(k - target))[:6]


def find_spread(tk: str, entry: pd.Timestamp, spot: float):
    """Locate a real 0.95/0.85 put spread with entry-day bars on both legs."""
    fridays = [entry + timedelta(days=n) for n in range(23, 40)]
    fridays = sorted((d for d in fridays if d.weekday() == 4),
                     key=lambda d: abs((d - entry).days - DTE))
    for exp in fridays[:3]:
        for ks in candidate_strikes(spot * SHORT_K):
            for kl in candidate_strikes(spot * LONG_K):
                if kl >= ks:
                    continue
                s_sym, l_sym = occ(tk, exp, ks), occ(tk, exp, kl)
                bars = get_bars([s_sym, l_sym], entry - timedelta(days=3),
                                min(exp + timedelta(days=1), pd.Timestamp.now()))
                bs_, bl_ = bars[s_sym], bars[l_sym]
                if entry in bs_.index and entry in bl_.index:
                    credit = float(bs_.loc[entry] - bl_.loc[entry])
                    if credit > 0.03:
                        return {"exp": exp, "ks": ks, "kl": kl,
                                "bars_s": bs_, "bars_l": bl_,
                                "credit": credit * (1 - HAIRCUT)}
                break  # only nearest long strike per short strike
    return None


def main():
    print("Loading underlying prices...")
    px = {}
    for t in LEADERS + ["SPY"]:
        s = yf.Ticker(t).history(start="2023-01-01", auto_adjust=True)["Close"]
        s = ensure_utc_naive(s)
        px[t] = s
    px = pd.DataFrame(px).ffill()
    ma50, ma200 = px.rolling(50).mean(), px.rolling(200).mean()
    dates = px.index[px.index >= START]

    equity, curve, open_pos = 100_000.0, {}, []
    n_trades = wins = real_fills = synth_skipped = 0
    real_marks = total_marks = 0

    rebal_marks = set(dates[::REBAL])
    for d in dates:
        row = px.loc[d]
        # ── MTM / exits on real closes ──
        still = []
        for p in open_pos:
            v = None
            if d in p["bars_s"].index and d in p["bars_l"].index:
                v = float(p["bars_s"].loc[d] - p["bars_l"].loc[d])
                p["last_v"] = v; real_marks += 1
            else:
                v = p.get("last_v", p["credit"])
            total_marks += 1
            pnl = (p["credit"] - v * (1 + HAIRCUT)) * p["mult"]
            p["pnl"] = pnl
            frac = pnl / p["credit_total"] if p["credit_total"] else 0
            if frac >= TP or frac <= SL or (p["exp"] - d).days <= 2:
                equity += pnl; n_trades += 1; wins += pnl > 0
            else:
                still.append(p)
        open_pos = still

        # ── rebalance / entries ──
        if d in rebal_marks:
            for p in open_pos:
                equity += p.get("pnl", 0.0); n_trades += 1
                wins += p.get("pnl", 0) > 0
            open_pos = []
            hist = px.loc[:d]
            cands = []
            for tk in LEADERS:
                s = row.get(tk, np.nan)
                if (np.isnan(s) or np.isnan(ma200[tk].loc[d])
                        or not (s > ma50[tk].loc[d] > ma200[tk].loc[d])):
                    continue
                mom = s / hist[tk].iloc[-21] - 1
                if mom > 0:
                    cands.append((mom, tk))
            for _, tk in sorted(cands, reverse=True)[:MAX_POS]:
                sp = find_spread(tk, d, float(row[tk]))
                if sp is None:
                    synth_skipped += 1
                    continue
                real_fills += 1
                max_loss = (sp["ks"] - sp["kl"]) - sp["credit"]
                mult = (equity * RISK_PCT) / max_loss if max_loss > 0 else 0
                if mult <= 0:
                    continue
                sp.update(tk=tk, mult=mult, credit_total=sp["credit"] * mult, d0=d)
                open_pos.append(sp)

        curve[d] = equity + sum(p.get("pnl", 0.0) for p in open_pos)

    eq = pd.Series(curve).sort_index()
    r_d = eq.pct_change().dropna()
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    sharpe = r_d.mean() / r_d.std() * np.sqrt(252) if r_d.std() > 0 else 0
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1
    dd = abs((eq / eq.cummax() - 1).min())

    print("\n" + "=" * 72)
    print("REAL-CHAIN OOS VALIDATION — leader put spreads (2024-02 → now)")
    print("=" * 72)
    print(f"  Sharpe {sharpe:5.2f}   CAGR {cagr*100:+6.1f}%   MaxDD {dd*100:5.1f}%")
    print(f"  trades {n_trades} (~{n_trades/yrs:.0f}/yr)   win {wins/max(n_trades,1)*100:.0f}%")
    print(f"  fills: {real_fills} real-chain, {synth_skipped} skipped (no chain data)")
    print(f"  marks: {real_marks}/{total_marks} real ({real_marks/max(total_marks,1)*100:.0f}%)")
    print(f"\n  synthetic-sim reference on same window: Sharpe 0.97")
    print(f"  gate: OOS Sharpe >= 1.5 → {'✅ PASS' if sharpe >= 1.5 else '❌ FAIL'}")
    eq.to_csv(ROOT / "results" / "putspread_realchain_oos_curve.csv")
    print(f"\nSaved → results/putspread_realchain_oos_curve.csv")


if __name__ == "__main__":
    main()
