#!/usr/bin/env python3
"""Empirical test: buying 1DTE calls on single names (META, NVDA).

Question (user): does buying Thursday->Friday expiry calls on high-beta
single names make money?

Method — REAL data only (Alpaca OPRA 5-min option bars, ~Feb 2024+):
  * Each Thursday: stock price at ~15:50 ET -> pick ATM and ~+2% OTM call
    expiring Friday. Entry = last 5-min bar close Thursday (+ spread
    penalty). Exit = Friday last bar close, floored at expiry intrinsic
    (computed from Friday stock close).
  * Contracts with no Thursday trades = untradeable -> skipped, counted.
  * Variants: unconditional; momentum-conditioned (stock day return >= +1%).

Output: per-name/per-variant win rate, avg/median return per trade,
total P&L on fixed $ per trade, worst/best, skip rate.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for line in (ROOT / ".env").read_text().splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

HDR = {"APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"],
       "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]}
DATA = "https://data.alpaca.markets"

SPREAD_PENALTY = 0.02   # $ per share each way (verified ~1-2c on liquid ATM)
NAMES = {"META": 10.0, "NVDA": 2.5}   # strike grid guess (refined per query)
START = date(2024, 2, 12)


def get(url: str) -> dict:
    req = urllib.request.Request(url, headers=HDR)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def stock_bars(sym: str, start: date, end: date) -> dict:
    """date -> {close_1550, close_eod} from 5-min bars (SIP feed via iex fallback)."""
    out = {}
    url = (f"{DATA}/v2/stocks/{sym}/bars?timeframe=5Min&adjustment=split"
           f"&start={start}T13:30:00Z&end={end}T21:00:00Z&limit=10000&feed=iex")
    page = None
    while True:
        u = url + (f"&page_token={page}" if page else "")
        d = get(u)
        for b in d.get("bars", []):
            ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
            day = ts.date()
            rec = out.setdefault(day, {})
            hhmm = ts.strftime("%H:%M")
            if hhmm == "19:50":            # 15:50 ET during EDT; 20:50 in EST
                rec["c1550"] = b["c"]
            if hhmm == "20:50":
                rec.setdefault("c1550", b["c"])   # EST months
            rec["eod"] = b["c"]                    # last bar wins
        page = d.get("next_page_token")
        if not page:
            break
    return out


def occ(sym: str, exp: date, strike: float) -> str:
    return f"{sym}{exp:%y%m%d}C{int(round(strike * 1000)):08d}"


def option_bars_multi(symbols: list[str], start: date, end: date) -> dict:
    q = urllib.parse.quote(",".join(symbols))
    url = (f"{DATA}/v1beta1/options/bars?symbols={q}&timeframe=5Min"
           f"&start={start}T13:30:00Z&end={end}T21:30:00Z&limit=10000")
    try:
        return get(url).get("bars", {})
    except Exception:
        return {}


def run_name(sym: str, grid: float) -> list[dict]:
    today = date.today()
    px = stock_bars(sym, START, today)
    trading_days = sorted(px)
    prev_close = {trading_days[i]: px[trading_days[i - 1]]["eod"]
                  for i in range(1, len(trading_days))}
    trades = []
    d = START
    while d <= today - timedelta(days=2):
        if d.weekday() == 3 and d in px and "c1550" in px[d]:   # Thursday
            fri = d + timedelta(days=1)
            if fri not in px:                                    # Friday holiday
                d += timedelta(days=1); continue
            spot = px[d]["c1550"]
            # candidate strikes: ATM (nearest grid) and ~+2% OTM
            atm = round(spot / grid) * grid
            otm = round(spot * 1.02 / grid) * grid
            if otm == atm:
                otm = atm + grid
            cands = {occ(sym, fri, k): k for k in {atm, otm}}
            bars = option_bars_multi(list(cands), d, fri)
            for osym, k in cands.items():
                obars = bars.get(osym, [])
                thu = [b for b in obars if b["t"][:10] == str(d)]
                frid = [b for b in obars if b["t"][:10] == str(fri)]
                if not thu:
                    trades.append({"sym": sym, "date": str(d), "k": k,
                                   "kind": "ATM" if k == atm else "OTM",
                                   "skipped": True})
                    continue
                entry = thu[-1]["c"] + SPREAD_PENALTY
                intrinsic = max(px[fri]["eod"] - k, 0.0)
                exit_px = max((frid[-1]["c"] - SPREAD_PENALTY) if frid else 0.0,
                              intrinsic)
                trades.append({
                    "sym": sym, "date": str(d), "k": k,
                    "kind": "ATM" if k == atm else "OTM",
                    "spot": round(spot, 2), "entry": round(entry, 2),
                    "exit": round(exit_px, 2),
                    "ret": round(exit_px / entry - 1, 4) if entry > 0 else None,
                    "thu_day_ret": (round(spot / prev_close[d] - 1, 4)
                                    if d in prev_close else None),
                    "fri_stock_ret": round(px[fri]["eod"] / spot - 1, 4),
                    "skipped": False,
                })
        d += timedelta(days=1)
    return trades


def summarize(trades: list[dict], label: str) -> None:
    live = [t for t in trades if not t.get("skipped") and t.get("ret") is not None]
    skips = sum(1 for t in trades if t.get("skipped"))
    if not live:
        print(f"  {label}: no tradeable weeks (skips={skips})")
        return
    import statistics as st
    rets = [t["ret"] for t in live]
    wins = sum(1 for r in rets if r > 0)
    # $500 risked per trade, compounding ignored (fixed bet)
    pnl = sum(500 * r for r in rets)
    print(f"  {label:26s} n={len(live):3d} skip={skips:3d} "
          f"win={wins/len(live):5.1%} avg={st.mean(rets):+7.1%} "
          f"med={st.median(rets):+7.1%} best={max(rets):+7.1%} "
          f"worst={min(rets):+7.1%} | $500/trade PnL=${pnl:+,.0f}")


if __name__ == "__main__":
    all_out = {}
    for sym, grid in NAMES.items():
        print(f"\n{sym} — Thursday buy 1DTE call, exit Friday close/intrinsic")
        tr = run_name(sym, grid)
        all_out[sym] = tr
        for kind in ("ATM", "OTM"):
            sel = [t for t in tr if t["kind"] == kind]
            summarize(sel, f"{kind} unconditional")
            mom = [t for t in sel if (t.get("thu_day_ret") or 0) >= 0.01]
            summarize(mom, f"{kind} momentum (thu>=+1%)")
        out = ROOT / "results" / "test_1dte_calls_raw.json"
        out.write_text(json.dumps(all_out, indent=1))
    print(f"\nraw trades → results/test_1dte_calls_raw.json")
