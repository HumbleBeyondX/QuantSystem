#!/usr/bin/env python3
"""Funding-rate carry — hedged pair, MAKER entries, live shadow runner.

Owner sleeve #2 (gate waived, rails instead). The edge observed
2026-07-12: SKHYNIX perp funding +62.9bp/8h — longs pay shorts ~1.9%/day.
Naked short = squeeze bait (7% day vol), so we run a HEDGED pair:

    SHORT the high-funding token  (collects funding)
    LONG  a correlated hedge token (pays ~0 funding)

Maker discipline (owner mandate): entries/exits post at the passive side
and only count as filled when the market trades THROUGH the quote
(conservative 5m-bar rule: sell fills if bar high >= quote; buy fills if
bar low <= quote). Unfilled quotes are re-posted each bar. Emergency
unwind (pair-loss stop) is the ONLY taker exception — safety beats fees.

Rails:
  * virtual sleeve 5,000 USDT; per-leg notional = 1.0 x sleeve
    (at 20x that is ~5% margin per leg — liquidation needs ~90% pair
    divergence; funding is the P&L engine, not direction)
  * enter only while funding >= MIN_FUNDING; unwind after 2 consecutive
    readings below EXIT_FUNDING
  * pair-loss stop: unwind (taker) if net pair P&L < -2% of sleeve
State:  data/live_state/funding_carry_state.json
Logs:   logs/paper_trading/funding_carry_daily.jsonl / _trades.jsonl
Run:    python -u scripts/funding_carry_runner.py [--once]
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
# Funding spikes on these tokens are PULSES (SKHYNIX: +63bp -> -3bp/8h
# within 30h). This runner is a patience trap: scan the whole watchlist
# every pass, quote in on whichever token's funding is richest above
# threshold, idle otherwise.
WATCHLIST = ["SPCX", "SKHYNIX", "NVDA", "TSLA", "HOOD", "MSTR", "CRCL",
             "WDC", "UVXY", "USO", "XLE", "URNM", "USAR"]
DEFAULT_HEDGE = "NVDA"          # liquid, ~zero funding
HEDGE_MAP = {"SKHYNIX": "NVDA", "MSTR": "CRCL", "NVDA": "TSLA"}
PAIRS = {}                      # filled dynamically each pass
SLEEVE = 5_000.0
LEG_NOTIONAL = 5_000.0          # per leg, = 5% margin at 20x
MIN_FUNDING = 0.0015            # 15bp/8h to enter
EXIT_FUNDING = 0.0005           # unwind after 2 readings below 5bp
PAIR_STOP = 0.02                # -2% of sleeve -> emergency taker unwind
MAKER_FEE = 0.0002
TAKER_FEE = 0.0005

STATE = ROOT / "data" / "live_state" / "funding_carry_state.json"
DAILY = ROOT / "logs" / "paper_trading" / "funding_carry_daily.jsonl"
TRADES = ROOT / "logs" / "paper_trading" / "funding_carry_trades.jsonl"


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
    return {"equity": SLEEVE, "pair": None, "quotes": None,
            "low_funding_count": 0, "funding_collected": 0.0,
            "n_rounds": 0}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


def book(ex, base: str):
    ob = ex.fetch_order_book(f"{base}/USDT:USDT", limit=5)
    bid = ob["bids"][0][0] if ob["bids"] else None
    ask = ob["asks"][0][0] if ob["asks"] else None
    return bid, ask


def last_bar_extremes(ex, base: str):
    o = ex.fetch_ohlcv(f"{base}/USDT:USDT", "5m", limit=2)
    b = o[-2] if len(o) >= 2 else o[-1]
    return b[2], b[3]           # high, low


def scan_richest(ex):
    """Highest current funding across the watchlist."""
    best, best_f = None, 0.0
    for base in WATCHLIST:
        try:
            f = float(ex.fetch_funding_rate(f"{base}/USDT:USDT")
                      .get("fundingRate") or 0)
            if f > best_f:
                best, best_f = base, f
            time.sleep(0.1)
        except Exception:
            continue
    return best, best_f


def run_pass(ex, st: dict) -> None:
    # dynamic pair selection: hold current pair if open, else scan
    if st["pair"] is not None:
        pairs = {st["pair"]["short_base"]: st["pair"]["long_base"]}
    else:
        richest, f = scan_richest(ex)
        if richest is None or f < MIN_FUNDING:
            append(DAILY, {"ts": now_iso(), "date": now_iso()[:10],
                           "logged_at": now_iso(), "pair_open": False,
                           "richest": richest,
                           "richest_bp8h": round(f * 1e4, 2) if richest else None,
                           "total_equity": round(st["equity"], 2),
                           "funding_collected_cum": round(st["funding_collected"], 2)})
            save_state(st)
            say(f"idle: richest funding {richest}={f*1e4:.1f}bp/8h "
                f"< {MIN_FUNDING*1e4:.0f}bp threshold")
            return
        hedge = HEDGE_MAP.get(richest, DEFAULT_HEDGE)
        if hedge == richest:
            hedge = DEFAULT_HEDGE if richest != DEFAULT_HEDGE else "TSLA"
        pairs = {richest: hedge}
    for short_base, long_base in pairs.items():
        try:
            fr = ex.fetch_funding_rate(f"{short_base}/USDT:USDT")
            funding = float(fr.get("fundingRate") or 0)
            fr_ts = fr.get("fundingTimestamp")
            sb, sa = book(ex, short_base)
            lb, la = book(ex, long_base)
            if not all((sb, sa, lb, la)):
                continue
            pair = st["pair"]

            # ── no position: quote in when funding is rich ──
            if pair is None and funding >= MIN_FUNDING:
                q = st.get("quotes")
                if not q or q.get("short_base") != short_base:
                    st["quotes"] = q = {
                        "short_base": short_base, "long_base": long_base,
                        "short_px": sa, "long_px": lb,       # passive side
                        "posted": now_iso(), "bars": 0,
                        "short_filled": False, "long_filled": False}
                    say(f"quoting: SHORT {short_base}@{sa} / LONG {long_base}@{lb} "
                        f"(funding {funding*1e4:.1f}bp/8h)")
                else:
                    hi_s, _ = last_bar_extremes(ex, short_base)
                    _, lo_l = last_bar_extremes(ex, long_base)
                    if not q["short_filled"] and hi_s >= q["short_px"]:
                        q["short_filled"] = True
                        say(f"{short_base}: short leg FILLED (maker) @ {q['short_px']}")
                    if not q["long_filled"] and lo_l <= q["long_px"]:
                        q["long_filled"] = True
                        say(f"{long_base}: long leg FILLED (maker) @ {q['long_px']}")
                    q["bars"] += 1
                    if q["short_filled"] and q["long_filled"]:
                        s_qty = LEG_NOTIONAL / q["short_px"]
                        l_qty = LEG_NOTIONAL / q["long_px"]
                        fee = LEG_NOTIONAL * MAKER_FEE * 2
                        st["equity"] -= fee
                        st["pair"] = {
                            "short_base": short_base, "long_base": long_base,
                            "s_entry": q["short_px"], "l_entry": q["long_px"],
                            "s_qty": s_qty, "l_qty": l_qty,
                            "opened": now_iso(), "last_funding_ts": fr_ts,
                            "funding_pnl": 0.0}
                        st["quotes"] = None
                        append(TRADES, {"ts": now_iso(), "action": "OPEN_PAIR",
                                        **st["pair"], "entry_funding": funding})
                        say(f"PAIR OPEN: -{short_base} +{long_base}, "
                            f"fees {fee:.2f} (maker x2)")
                    elif q["bars"] > 12:      # 1h unfilled -> requote
                        st["quotes"] = None

            # ── position open: accrue funding, watch exits ──
            elif pair is not None:
                s_mark = (sb + sa) / 2
                l_mark = (lb + la) / 2
                price_pnl = ((pair["s_entry"] - s_mark) * pair["s_qty"]
                             + (l_mark - pair["l_entry"]) * pair["l_qty"])
                # funding accrual when a new funding period stamps
                if fr_ts and fr_ts != pair["last_funding_ts"]:
                    accr = funding * pair["s_qty"] * s_mark
                    l_fr = ex.fetch_funding_rate(f"{long_base}/USDT:USDT")
                    l_funding = float(l_fr.get("fundingRate") or 0)
                    accr -= l_funding * pair["l_qty"] * l_mark
                    pair["funding_pnl"] += accr
                    st["funding_collected"] += accr
                    pair["last_funding_ts"] = fr_ts
                    say(f"funding accrued: {accr:+.2f} "
                        f"(cum {pair['funding_pnl']:+.2f})")
                net = price_pnl + pair["funding_pnl"]

                st["low_funding_count"] = (st["low_funding_count"] + 1
                                           if funding < EXIT_FUNDING else 0)
                unwind = None
                if net < -PAIR_STOP * SLEEVE:
                    unwind = ("pair_stop_TAKER", TAKER_FEE)
                elif st["low_funding_count"] >= 2:
                    unwind = ("funding_gone_maker", MAKER_FEE)
                if unwind:
                    reason, fee_rate = unwind
                    exit_fee = (pair["s_qty"] * s_mark
                                + pair["l_qty"] * l_mark) * fee_rate
                    realized = net - exit_fee
                    st["equity"] += realized
                    st["n_rounds"] += 1
                    append(TRADES, {"ts": now_iso(), "action": "CLOSE_PAIR",
                                    "reason": reason,
                                    "price_pnl": round(price_pnl, 2),
                                    "funding_pnl": round(pair["funding_pnl"], 2),
                                    "realized": round(realized, 2)})
                    say(f"PAIR CLOSE ({reason}): price {price_pnl:+.2f} "
                        f"funding {pair['funding_pnl']:+.2f} net {realized:+.2f}")
                    st["pair"] = None
                    st["low_funding_count"] = 0

            append(DAILY, {
                "ts": now_iso(), "date": now_iso()[:10], "logged_at": now_iso(),
                "funding_bp8h": round(funding * 1e4, 2),
                "pair_open": st["pair"] is not None,
                "total_equity": round(st["equity"] + (
                    ((st["pair"]["s_entry"] - (sb + sa) / 2) * st["pair"]["s_qty"]
                     + ((lb + la) / 2 - st["pair"]["l_entry"]) * st["pair"]["l_qty"]
                     + st["pair"]["funding_pnl"]) if st["pair"] else 0), 2),
                "funding_collected_cum": round(st["funding_collected"], 2),
            })
        except Exception as e:
            say(f"{short_base}: pass failed {type(e).__name__}: {str(e)[:60]}")
    save_state(st)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    say("importing ccxt...")
    import ccxt
    ex = ccxt.okx({"options": {"defaultType": "swap"}, "timeout": 20000})
    ex.load_markets()
    say(f"carry runner up: pairs={PAIRS} leg=${LEG_NOTIONAL:,.0f} "
        f"enter>={MIN_FUNDING*1e4:.0f}bp exit<{EXIT_FUNDING*1e4:.0f}bp "
        f"stop=-{PAIR_STOP:.0%} (VIRTUAL, maker fills only)")
    st = load_state()
    while True:
        run_pass(ex, st)
        if args.once:
            break
        time.sleep(300 - (time.time() % 300) + 10)


if __name__ == "__main__":
    main()
