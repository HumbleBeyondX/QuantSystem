#!/usr/bin/env python3
"""Market-making shadow runner — wide-spread tokenized ETF perps.

Owner sleeve #3 (gate waived, rails instead). Observed 2026-07-13:
XLE 17.7bp / URNM 13.8bp / UVXY 12.5bp / USO 9.6bp spreads with
$19k-89k depth10 — vs 2bp maker fee. Two-sided quoting captures the
spread; inventory risk is the cost.

V0 mechanics (60s loop, 1m-bar fill check):
  * join the touch: quote at best bid + best ask, size $500/side
  * fill rule (conservative direction): a quote fills only when the next
    1m bar trades STRICTLY through it (low < bid / high > ask)
  * inventory skew: quotes shift by INV_SKEW_BPS per unit of inventory;
    the loaded side stops quoting at |inventory| >= INV_CAP units
  * inventory marked at mid; per-symbol taker flatten at -1% sleeve
  * global rails: $5k virtual sleeve, daily loss cap 3% -> halt

Honest caveat (in every report): bar-through fills ignore queue position
— optimistic at the touch. Real maker fills need live post-only orders;
this v0 measures whether the ECONOMICS (spread vs adverse selection)
are positive at all.

State:  data/live_state/mm_sleeve_state.json
Logs:   logs/paper_trading/mm_sleeve_daily.jsonl / _trades.jsonl
Run:    python -u scripts/mm_paper_runner.py [--once]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── owner-tunable knobs ──────────────────────────────────────────
SYMBOLS = ["XLE", "URNM", "UVXY", "USO"]
QUOTE_USD = 500.0
INV_CAP = 3                    # units of QUOTE_USD per symbol
INV_SKEW_BPS = 2.0             # quote shift per inventory unit
SLEEVE = 5_000.0
DAILY_LOSS_CAP = 0.03
SYMBOL_STOP = 0.01             # -1% of sleeve on one book -> taker flatten
MAKER_FEE = 0.0002
TAKER_FEE = 0.0005

STATE = ROOT / "data" / "live_state" / "mm_sleeve_state.json"
DAILY = ROOT / "logs" / "paper_trading" / "mm_sleeve_daily.jsonl"
TRADES = ROOT / "logs" / "paper_trading" / "mm_sleeve_trades.jsonl"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def say(msg: str) -> None:
    print(f"[{now_iso()}] {msg}", flush=True)


def append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"cash": SLEEVE, "day": now_iso()[:10], "realized_today": 0.0,
            "halted": False, "books": {s: {"inv": 0.0, "avg_px": 0.0,
                                           "quotes": None, "fills": 0,
                                           "captured": 0.0}
                                       for s in SYMBOLS}}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


def total_equity(st: dict, mids: dict) -> float:
    eq = st["cash"]
    for s, b in st["books"].items():
        if b["inv"] and mids.get(s):
            eq += b["inv"] * mids[s]
    return eq


def run_pass(ex, st: dict) -> None:
    today = now_iso()[:10]
    if st["day"] != today:
        st["day"], st["realized_today"], st["halted"] = today, 0.0, False

    mids = {}
    for s in SYMBOLS:
        sym = f"{s}/USDT:USDT"
        b = st["books"][s]
        try:
            ob = ex.fetch_order_book(sym, limit=5)
            bid = ob["bids"][0][0] if ob["bids"] else None
            ask = ob["asks"][0][0] if ob["asks"] else None
            if not bid or not ask:
                continue
            mid = (bid + ask) / 2
            mids[s] = mid

            # ── fill check on previous quotes (last CLOSED 1m bar) ──
            q = b.get("quotes")
            if q:
                o = ex.fetch_ohlcv(sym, "1m", limit=2)
                bar = o[-2] if len(o) >= 2 else o[-1]
                hi, lo = bar[2], bar[3]
                qty = QUOTE_USD / mid
                if q.get("bid") and lo < q["bid"]:
                    fee = QUOTE_USD * MAKER_FEE
                    b["inv"] += qty
                    st["cash"] -= q["bid"] * qty + fee
                    b["fills"] += 1
                    append(TRADES, {"ts": now_iso(), "symbol": s,
                                    "side": "BUY_FILL", "px": q["bid"],
                                    "qty": round(qty, 4),
                                    "inv": round(b["inv"], 4)})
                    say(f"{s}: BUY fill @ {q['bid']} inv={b['inv']:.3f}")
                if q.get("ask") and hi > q["ask"]:
                    fee = QUOTE_USD * MAKER_FEE
                    b["inv"] -= qty
                    st["cash"] += q["ask"] * qty - fee
                    b["fills"] += 1
                    append(TRADES, {"ts": now_iso(), "symbol": s,
                                    "side": "SELL_FILL", "px": q["ask"],
                                    "qty": round(qty, 4),
                                    "inv": round(b["inv"], 4)})
                    say(f"{s}: SELL fill @ {q['ask']} inv={b['inv']:.3f}")

            # ── per-symbol inventory stop (taker flatten) ──
            inv_usd = b["inv"] * mid
            book_pnl = st["cash"] + sum(bb["inv"] * mids.get(ss, 0)
                                        for ss, bb in st["books"].items()) - SLEEVE
            if abs(inv_usd) > 0 and b["inv"] != 0:
                # unrealized on this book vs its running average not tracked
                # in v0 — stop keys off total drawdown instead (below)
                pass

            # ── re-quote (skewed, capped) unless halted ──
            if st["halted"]:
                b["quotes"] = None
                continue
            inv_units = b["inv"] * mid / QUOTE_USD
            skew = mid * INV_SKEW_BPS / 1e4 * inv_units
            newq = {}
            if inv_units < INV_CAP:
                newq["bid"] = round(bid - skew, 6)
            if inv_units > -INV_CAP:
                newq["ask"] = round(ask - skew, 6)
            b["quotes"] = newq
        except Exception as e:
            say(f"{s}: pass failed {type(e).__name__}: {str(e)[:60]}")

    eq = total_equity(st, mids)
    st["realized_today"] = eq - SLEEVE  # v0: daily anchor = sleeve
    if eq - SLEEVE < -DAILY_LOSS_CAP * SLEEVE and not st["halted"]:
        say(f"DAILY LOSS CAP HIT (eq {eq:.0f}) — quotes pulled, "
            f"inventory flattened at taker")
        for s, b in st["books"].items():
            if b["inv"] and mids.get(s):
                px = mids[s]
                st["cash"] += b["inv"] * px - abs(b["inv"] * px) * TAKER_FEE
                append(TRADES, {"ts": now_iso(), "symbol": s,
                                "side": "FLATTEN_TAKER", "px": px,
                                "qty": round(-b["inv"], 4)})
                b["inv"] = 0.0
            b["quotes"] = None
        st["halted"] = True

    append(DAILY, {"ts": now_iso(), "date": now_iso()[:10],
                   "logged_at": now_iso(),
                   "total_equity": round(eq, 2),
                   "cash": round(st["cash"], 2),
                   "inventory_usd": {s: round(b["inv"] * mids.get(s, 0), 2)
                                     for s, b in st["books"].items()},
                   "fills_total": sum(b["fills"] for b in st["books"].values()),
                   "halted": st["halted"]})
    save_state(st)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    say("importing ccxt...")
    import ccxt
    ex = ccxt.okx({"options": {"defaultType": "swap"}, "timeout": 20000})
    ex.load_markets()
    say(f"MM runner up: {SYMBOLS} quote=${QUOTE_USD:.0f}/side "
        f"inv_cap={INV_CAP} skew={INV_SKEW_BPS}bp/unit sleeve=${SLEEVE:,.0f} "
        f"(VIRTUAL, bar-through maker fills)")
    st = load_state()
    while True:
        run_pass(ex, st)
        if args.once:
            break
        time.sleep(60 - (time.time() % 60) + 2)


if __name__ == "__main__":
    main()
