#!/usr/bin/env python3
"""PEAD follow-ups: (A) sell put spreads after reaction; (B) does ex-ante
momentum select the payers; (C) ex-ante momo-stock lottery sleeve.

User mandate for (C): SMALL fixed size, objective = high-multiple payoffs,
NOT Sharpe. Metrics: hit rates of >100%/>300% winners, total multiple,
losing streaks, bankroll simulation at 0.5%/1% per bet.

Real data only: Alpaca OPRA 5-min option bars, yfinance earnings dates,
sp500 panel (153 names, mild survivorship — disclosed) for momentum ranks.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
import warnings
from datetime import date, timedelta
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


def get(url: str) -> dict:
    req = urllib.request.Request(url, headers=HDR)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def occ(sym: str, exp: date, strike: float, cp: str = "C") -> str:
    return f"{sym}{exp:%y%m%d}{cp}{int(round(strike * 1000)):08d}"


def option_bars(symbols: list[str], start: date, end: date) -> dict:
    q = urllib.parse.quote(",".join(symbols))
    url = (f"{DATA}/v1beta1/options/bars?symbols={q}&timeframe=5Min"
           f"&start={start}T13:30:00Z&end={end + timedelta(days=1)}T00:00:00Z&limit=10000")
    time.sleep(0.15)
    try:
        return get(url).get("bars", {})
    except Exception:
        return {}


def week_friday(d: date) -> date:
    return d + timedelta(days=4 - d.weekday()) if d.weekday() <= 4 else d + timedelta(days=(4 - d.weekday()) % 7 or 7)


def day_bars(bars: dict, osym: str, d: date) -> list:
    return [b for b in bars.get(osym, []) if b["t"][:10] == str(d)]


# ── A: sell put spread after reaction ─────────────────────────────

def put_spread_trade(sym: str, R: date, spot: float, spot_exp: float,
                     expiry: date) -> dict | None:
    for g in (1.0, 2.5, 5.0, 10.0):
        k_short = int(spot * 0.995 / g) * g
        k_long = k_short - 2 * g
        if k_long <= 0:
            continue
        legs = [occ(sym, expiry, k_short, "P"), occ(sym, expiry, k_long, "P")]
        bars = option_bars(legs, R, expiry)
        sb, lb = day_bars(bars, legs[0], R), day_bars(bars, legs[1], R)
        if not sb or not lb:
            continue
        credit = (sb[-1]["c"] - PENALTY) - (lb[-1]["c"] + PENALTY)
        width = 2 * g
        if credit < 0.03 or credit >= width:
            continue
        loss = max(k_short - spot_exp, 0) - max(k_long - spot_exp, 0)
        pnl = credit - loss
        return {"sym": sym, "reaction_day": str(R), "k_short": k_short,
                "width": width, "credit": round(credit, 2),
                "pnl": round(pnl, 2),
                "ret_on_risk": round(pnl / (width - credit), 4)}
    return None


# ── C: lottery sleeve on ex-ante momentum universe ────────────────

def atm_candidates(spot: float) -> list[float]:
    out = set()
    for g in (1.0, 2.5, 5.0, 10.0):
        base = round(spot / g) * g
        out.update({base, base + g, base - g})
    return sorted(k for k in out if k > 0)


def call_trade(sym: str, R: date, spot: float, spot_exp: float,
               expiry: date) -> dict | None:
    cands = {occ(sym, expiry, k): k for k in atm_candidates(spot)}
    bars = option_bars(list(cands), R, expiry)
    best, dist = None, 9e9
    for osym, k in cands.items():
        if day_bars(bars, osym, R) and abs(k - spot) < dist:
            best, dist = osym, abs(k - spot)
    if not best:
        return None
    entry = day_bars(bars, best, R)[-1]["c"] + PENALTY
    if entry <= 0.03:
        return None
    strike = int(best[-8:]) / 1000
    xb = day_bars(bars, best, expiry)
    exit_px = max((xb[-1]["c"] - PENALTY) if xb else 0.0,
                  max(spot_exp - strike, 0.0))
    return {"sym": sym, "reaction_day": str(R), "contract": best,
            "entry": round(entry, 2), "exit": round(exit_px, 2),
            "ret": round(exit_px / entry - 1, 4)}


def main() -> None:
    import pandas as pd
    import yfinance as yf

    # ── momentum panel (ex-ante ranks) ──
    panel = pd.read_parquet(ROOT / "data/sample/sp500_2015-01-01_2026-05-04.parquet")
    closes = panel.pivot_table(index="timestamp", columns="ticker",
                               values="close", aggfunc="last").sort_index()
    mom6 = closes.pct_change(126)

    # megacap event set: reuse prior raw (reaction days already computed)
    prior = json.load(open(ROOT / "results/earnings_1dte_calls_raw.json"))
    events = [(t["sym"], date.fromisoformat(t["reaction_day"]), t["reaction_ret"])
              for t in prior["pead"]]

    px_syms = sorted({s for s, _, _ in events})
    px = yf.download(px_syms, start="2024-01-01", progress=False,
                     auto_adjust=True)["Close"]
    px.index = pd.to_datetime(px.index).date

    # ── A: put spreads on the same reaction set ──
    print("=== A: sell 2-grid put spread at reaction close, hold to expiry ===")
    spreads = []
    for sym, R, rr in events:
        expiry = week_friday(R) if week_friday(R) > R else week_friday(R + timedelta(days=3))
        s = px[sym].dropna()
        if R not in s.index:
            continue
        later = [d for d in s.index if d >= expiry]
        spot_exp = float(s[later[0]]) if later else float(s[max(d for d in s.index)])
        tr = put_spread_trade(sym, R, float(s[R]), spot_exp, expiry)
        if tr:
            tr["reaction_ret"] = rr
            spreads.append(tr)
    if spreads:
        import statistics as st
        rr = [t["ret_on_risk"] for t in spreads]
        wins = sum(1 for r in rr if r > 0)
        print(f" n={len(spreads)} win={wins/len(rr):.0%} avg_ret_on_risk={st.mean(rr):+.1%} "
              f"med={st.median(rr):+.1%} worst={min(rr):+.1%} sum={sum(rr):+.2f}")
        by_year = {}
        for t in spreads:
            by_year.setdefault(t["reaction_day"][:4], []).append(t["ret_on_risk"])
        for y in sorted(by_year):
            print(f"   {y}: n={len(by_year[y])} avg={st.mean(by_year[y]):+.1%} sum={sum(by_year[y]):+.2f}")

    # ── B: did ex-ante 6m momentum select the call payers? ──
    print("\n=== B: prior 58 call trades conditioned on ex-ante 6m momentum ===")
    rows = []
    for t in prior["pead"]:
        sym, R = t["sym"], pd.Timestamp(t["reaction_day"])
        if sym in mom6.columns:
            m = mom6[sym].loc[:R - pd.Timedelta(days=1)].dropna()
            if len(m):
                rows.append({"ret": t["ret"], "mom6": float(m.iloc[-1])})
    if rows:
        import statistics as st
        rows.sort(key=lambda r: r["mom6"])
        third = len(rows) // 3
        for lbl, grp in (("low-momo tercile", rows[:third]),
                         ("mid", rows[third:2 * third]),
                         ("HIGH-momo tercile", rows[2 * third:])):
            rets = [r["ret"] for r in grp]
            print(f" {lbl:18s} n={len(rets)} avg={st.mean(rets):+.1%} "
                  f"sum={sum(rets):+.2f} hits>100%={sum(r > 1 for r in rets)}")

    # ── C: ex-ante momo universe lottery sleeve ──
    print("\n=== C: quarterly top-15 6m-momentum names (ex megacap-tested), PEAD calls ===")
    tested = set(px_syms)
    universe = set()
    q_starts = pd.date_range("2024-01-01", "2026-05-01", freq="QS")
    for q in q_starts:
        m = mom6.loc[:q].dropna(how="all")
        if m.empty:
            continue
        ranks = m.iloc[-1].dropna().sort_values(ascending=False)
        universe.update([s for s in ranks.head(15).index if s not in tested])
    universe = sorted(universe)
    print(f" ex-ante momo pool ({len(universe)}): {','.join(universe)}")

    upx = yf.download(universe, start="2024-01-01", progress=False,
                      auto_adjust=True)["Close"]
    upx.index = pd.to_datetime(upx.index).date
    sleeve = []
    for sym in universe:
        try:
            ed = yf.Ticker(sym).earnings_dates
        except Exception:
            continue
        if ed is None or ed.empty or sym not in upx.columns:
            continue
        s = upx[sym].dropna()
        tdays = sorted(s.index)
        rets = {tdays[i]: float(s[tdays[i]] / s[tdays[i - 1]] - 1)
                for i in range(1, len(tdays))}
        for e in sorted({d.date() for d in ed.index if START <= d.date() <= END}):
            cands = [c for c in (e, min((t for t in tdays if t > e), default=None))
                     if c in rets]
            if not cands:
                continue
            R = max(cands, key=lambda c: abs(rets[c]))
            if rets[R] < 0.015:
                continue
            # was it in the ex-ante top-15 at the preceding quarter start?
            q = max((q.date() for q in q_starts if q.date() <= R), default=None)
            if q is None or sym not in mom6.columns:
                continue
            m = mom6[sym].loc[:pd.Timestamp(q)].dropna()
            allm = mom6.loc[:pd.Timestamp(q)].dropna(how="all")
            if m.empty or allm.empty:
                continue
            rank = (allm.iloc[-1] > m.iloc[-1]).sum() + 1
            if rank > 15:
                continue
            expiry = week_friday(R) if week_friday(R) > R else week_friday(R + timedelta(days=3))
            later = [d for d in tdays if d >= expiry]
            spot_exp = float(s[later[0]]) if later else float(s[tdays[-1]])
            tr = call_trade(sym, R, float(s[R]), spot_exp, expiry)
            if tr:
                tr["reaction_ret"] = round(rets[R], 4)
                tr["mom6_rank"] = int(rank)
                sleeve.append(tr)
        print(f"  {sym} done ({len(sleeve)} total)")

    out = {"put_spreads": spreads, "sleeve": sleeve}
    (ROOT / "results/pead_sleeve_raw.json").write_text(json.dumps(out, indent=1))

    if sleeve:
        import statistics as st
        rets = sorted((t["ret"] for t in sleeve), reverse=True)
        tot = sum(rets)
        print(f"\n sleeve: n={len(rets)} win={sum(r > 0 for r in rets)/len(rets):.0%} "
              f"avg={st.mean(rets):+.1%} med={st.median(rets):+.1%} sum={tot:+.2f} "
              f"ex-top1={tot-rets[0]:+.2f} ex-top3={tot-sum(rets[:3]):+.2f}")
        print(f" hits >+100%: {sum(r > 1 for r in rets)}  >+300%: {sum(r > 3 for r in rets)}  "
              f"max: {max(rets):+.0%}")
        # losing streak + bankroll sim (fixed fraction per bet)
        seq = [t["ret"] for t in sorted(sleeve, key=lambda t: t["reaction_day"])]
        streak = best = 0
        for r in seq:
            streak = streak + 1 if r <= 0 else 0
            best = max(best, streak)
        print(f" max losing streak: {best}")
        for frac in (0.005, 0.01):
            eq = 1.0
            for r in seq:
                eq *= (1 + frac * r)
            print(f" bankroll @{frac:.1%}/bet: x{eq:.3f} over {len(seq)} bets "
                  f"({(eq - 1) * 100:+.1f}% total)")


if __name__ == "__main__":
    main()
    print("\nraw → results/pead_sleeve_raw.json")
