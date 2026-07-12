#!/usr/bin/env python3
"""Catalyst-conditioned short-dated call buying — NDX leaders, earnings weeks.

Follow-up to test_1dte_calls.py (unconditional buying rejected: lottery
concentration — but ALL winners clustered on catalyst weeks). Hypothesis:
restrict buying to earnings-reaction weeks across the Nasdaq-100 leaders.

Variants (both real Alpaca OPRA 5-min option bars, entry +$0.02 penalty):
  V1 PRE-EVENT : buy same-week-Friday-expiry ATM call at the close BEFORE
                 the reaction day; sell at reaction-day close (or intrinsic
                 if that Friday). Pays the IV crush — the classic trap.
  V2 PEAD      : reaction day closes >= +1.5% -> buy nearest-Friday ATM
                 call at reaction-day close, hold to expiry (close floored
                 at intrinsic). IV already crushed; rides the drift.

Earnings dates: yfinance (real reported dates). Stock closes: yfinance
daily (auto-adjusted). Report ex-top-N robustness + per-year stability —
the lottery test the last strategy failed.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for line in (ROOT / ".env").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

HDR = {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
       "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}
DATA = "https://data.alpaca.markets"
PENALTY = 0.02
START, END = date(2024, 2, 12), date.today() - timedelta(days=3)
PEAD_MIN_REACTION = 0.015

UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA",
            "NFLX", "AMD", "COST", "ADBE", "QCOM", "AMAT", "MU", "CRM",
            "ORCL", "PLTR"]


def get(url: str) -> dict:
    req = urllib.request.Request(url, headers=HDR)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def occ(sym: str, exp: date, strike: float) -> str:
    return f"{sym}{exp:%y%m%d}C{int(round(strike * 1000)):08d}"


def option_bars(symbols: list[str], start: date, end: date) -> dict:
    q = urllib.parse.quote(",".join(symbols))
    url = (f"{DATA}/v1beta1/options/bars?symbols={q}&timeframe=5Min"
           f"&start={start}T13:30:00Z&end={end + timedelta(days=1)}T00:00:00Z&limit=10000")
    time.sleep(0.15)
    try:
        return get(url).get("bars", {})
    except Exception:
        return {}


def atm_candidates(spot: float) -> list[float]:
    out = set()
    for g in (1.0, 2.5, 5.0, 10.0):
        base = round(spot / g) * g
        out.update({base, base + g, base - g})
    return sorted(k for k in out if k > 0)


def next_friday(d: date) -> date:
    return d + timedelta(days=(4 - d.weekday()) % 7 or 7)


def week_friday(d: date) -> date:
    return d + timedelta(days=4 - d.weekday()) if d.weekday() <= 4 else next_friday(d)


def pick_contract(sym: str, spot: float, expiry: date, entry_day: date,
                  last_day: date) -> tuple[str | None, list, dict]:
    """Nearest-ATM listed contract WITH trades on entry day."""
    cands = {occ(sym, expiry, k): k for k in atm_candidates(spot)}
    bars = option_bars(list(cands), entry_day, last_day)
    best, best_dist = None, 9e9
    for osym, k in cands.items():
        day_bars = [b for b in bars.get(osym, []) if b["t"][:10] == str(entry_day)]
        if day_bars and abs(k - spot) < best_dist:
            best, best_dist = osym, abs(k - spot)
    return best, (bars.get(best, []) if best else []), cands


def run() -> dict:
    import pandas as pd
    import yfinance as yf

    px = yf.download(UNIVERSE, start="2024-01-01", progress=False,
                     auto_adjust=True)["Close"]
    px.index = pd.to_datetime(px.index).date
    tdays = sorted(px.index)

    def next_tday(d):
        for t in tdays:
            if t > d:
                return t
        return None

    def prev_tday(d):
        prev = None
        for t in tdays:
            if t >= d:
                return prev
            prev = t
        return prev

    trades = {"pre_event": [], "pead": []}
    n_events = 0
    for sym in UNIVERSE:
        try:
            ed = yf.Ticker(sym).earnings_dates
        except Exception:
            continue
        if ed is None or ed.empty:
            continue
        events = sorted({d.date() for d in ed.index
                         if START <= d.date() <= END})
        s = px[sym].dropna()
        rets = {}
        prev = None
        for t in sorted(s.index):
            if prev is not None:
                rets[t] = float(s[t] / s[prev] - 1)
            prev = t
        for e in events:
            # reaction day = bigger |move| of report day vs next trading day
            c1 = e if e in rets else None
            c2 = next_tday(e)
            cands = [c for c in (c1, c2) if c in rets]
            if not cands:
                continue
            R = max(cands, key=lambda c: abs(rets[c]))
            n_events += 1

            # ── V1 pre-event ──
            entry_day = prev_tday(R)
            expiry = week_friday(R)
            if entry_day and entry_day in s.index and expiry >= R:
                spot = float(s[entry_day])
                osym, bars, _ = pick_contract(sym, spot, expiry, entry_day, R)
                if osym:
                    ebars = [b for b in bars if b["t"][:10] == str(entry_day)]
                    rbars = [b for b in bars if b["t"][:10] == str(R)]
                    entry = ebars[-1]["c"] + PENALTY
                    strike = int(osym[-8:]) / 1000
                    intrinsic = max(float(s[R]) - strike, 0.0)
                    exit_px = (max((rbars[-1]["c"] - PENALTY) if rbars else 0.0,
                               intrinsic) if expiry == R
                               else ((rbars[-1]["c"] - PENALTY) if rbars else intrinsic))
                    if entry > 0.03:
                        trades["pre_event"].append({
                            "sym": sym, "event": str(e), "reaction_day": str(R),
                            "reaction_ret": round(rets[R], 4), "dte": (expiry - entry_day).days,
                            "contract": osym, "entry": round(entry, 2),
                            "exit": round(max(exit_px, 0.0), 2),
                            "ret": round(max(exit_px, 0.0) / entry - 1, 4)})

            # ── V2 PEAD ──
            if rets[R] >= PEAD_MIN_REACTION:
                entry_day = R
                expiry = week_friday(R) if week_friday(R) > R else next_friday(R)
                last = expiry if expiry in s.index else prev_tday(expiry) or expiry
                spot = float(s[entry_day])
                osym, bars, _ = pick_contract(sym, spot, expiry, entry_day, expiry)
                if osym:
                    ebars = [b for b in bars if b["t"][:10] == str(entry_day)]
                    xbars = [b for b in bars if b["t"][:10] == str(last)]
                    entry = ebars[-1]["c"] + PENALTY
                    strike = int(osym[-8:]) / 1000
                    spot_exp = float(s[last]) if last in s.index else spot
                    intrinsic = max(spot_exp - strike, 0.0)
                    exit_px = max((xbars[-1]["c"] - PENALTY) if xbars else 0.0,
                                  intrinsic)
                    if entry > 0.03:
                        trades["pead"].append({
                            "sym": sym, "event": str(e), "reaction_day": str(R),
                            "reaction_ret": round(rets[R], 4),
                            "dte": (expiry - entry_day).days,
                            "contract": osym, "entry": round(entry, 2),
                            "exit": round(exit_px, 2),
                            "ret": round(exit_px / entry - 1, 4)})
        print(f"  {sym}: {len(events)} events processed")
    print(f"\ntotal events: {n_events}")
    return trades


def summarize(rows: list[dict], label: str) -> None:
    import statistics as st
    if not rows:
        print(f"{label}: no trades")
        return
    rets = sorted((t["ret"] for t in rows), reverse=True)
    wins = sum(1 for r in rets if r > 0)
    tot = sum(rets)
    print(f"\n{label}: n={len(rets)} win={wins/len(rets):.1%} "
          f"avg={st.mean(rets):+.1%} med={st.median(rets):+.1%} "
          f"sum={tot:+.2f} ex-top1={tot-rets[0]:+.2f} ex-top3={tot-sum(rets[:3]):+.2f} "
          f"| $500/trade PnL=${500*tot:+,.0f}")
    by_year = {}
    for t in rows:
        by_year.setdefault(t["reaction_day"][:4], []).append(t["ret"])
    for y in sorted(by_year):
        r = by_year[y]
        print(f"   {y}: n={len(r)} avg={st.mean(r):+.1%} sum={sum(r):+.2f}")


if __name__ == "__main__":
    trades = run()
    out = ROOT / "results" / "earnings_1dte_calls_raw.json"
    out.write_text(json.dumps(trades, indent=1))
    summarize(trades["pre_event"], "V1 PRE-EVENT (buy before reaction, pay IV crush)")
    summarize(trades["pead"], "V2 PEAD (buy after +1.5% reaction, ride drift to Friday)")
    print(f"\nraw → results/earnings_1dte_calls_raw.json")
