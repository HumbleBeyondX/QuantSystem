#!/usr/bin/env python3
"""BTC Wheel DELTA-STRIKE paper trader — real Deribit chains, real greeks.

Validated 2026-07-12 (results/wheel_delta_strike_research.json): strikes
solved for target delta (put -0.40 / call 0.20) instead of fixed % of
spot. OOS Sharpe 2.07 under the full post-incident protocol.

This paper instance runs ALONGSIDE the fixed-strike wheel (separate
state/logs/schedule) — the fixed wheel's 8-week window is untouched.
Strike selection uses the chain's REAL greeks (Deribit ticker.greeks.delta),
not model inversion. Everything else reuses the validated shadow-fill
machinery from wheel_paper_trade.py.

State:  data/live_state/btc_wheel_delta_state.json
Logs:   logs/paper_trading/btc_wheel_delta_daily.jsonl / _trades.jsonl
Run:    python scripts/wheel_delta_paper_trade.py --mode once  (16:50 task)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.wheel_paper_trade import (  # noqa: E402
    NOTIONAL_PCT, append, get, index_price, leg_fee_usd, ticker, _now,
)

STATE = ROOT / "data" / "live_state" / "btc_wheel_delta_state.json"
DAILY = ROOT / "logs" / "paper_trading" / "btc_wheel_delta_daily.jsonl"
TRADES = ROOT / "logs" / "paper_trading" / "btc_wheel_delta_trades.jsonl"
INITIAL_EQUITY = 100_000.0
PUT_TARGET_DELTA = -0.40
CALL_TARGET_DELTA = 0.20


def pick_strangle_delta(idx: float) -> dict | None:
    """Nearest-Friday 5-9 DTE; strikes with REAL delta closest to targets."""
    instrs = get("get_instruments?currency=BTC&kind=option&expired=false")
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    rows = [{"name": i["instrument_name"], "k": float(i["strike"]),
             "cp": i["option_type"], "exp": i["expiration_timestamp"],
             "dte": (i["expiration_timestamp"] - now_ms) / 86_400_000}
            for i in instrs]
    cand = [r for r in rows if 5 <= r["dte"] <= 9]
    if not cand:
        cand = [r for r in rows if 4 <= r["dte"] <= 12]
    if not cand:
        return None
    exp = min({r["exp"] for r in cand},
              key=lambda e: abs(next(r["dte"] for r in cand if r["exp"] == e) - 7))

    # candidate strikes in a sane window; query REAL greeks per contract
    puts = sorted([r for r in cand if r["exp"] == exp and r["cp"] == "put"
                   and 0.80 * idx <= r["k"] <= 1.02 * idx],
                  key=lambda r: abs(r["k"] - idx))[:12]
    calls = sorted([r for r in cand if r["exp"] == exp and r["cp"] == "call"
                    and 0.98 * idx <= r["k"] <= 1.25 * idx],
                   key=lambda r: abs(r["k"] - idx))[:12]

    def best_by_delta(legs: list, target: float) -> dict | None:
        best, best_err = None, 9e9
        for r in legs:
            try:
                t = ticker(r["name"])
                delta = float((t.get("greeks") or {}).get("delta"))
            except Exception:
                continue
            err = abs(delta - target)
            if err < best_err:
                best, best_err = {**r, "delta": delta}, err
        return best

    p = best_by_delta(puts, PUT_TARGET_DELTA)
    c = best_by_delta(calls, CALL_TARGET_DELTA)
    if not p or not c:
        return None
    return {"put": p, "call": c, "expiry_ms": exp}


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"equity": INITIAL_EQUITY, "realized": 0.0, "position": None,
            "n_trades": 0, "wins": 0, "created": _now(),
            "variant": "delta_strike_0.40P_0.20C"}


def run_once() -> None:
    st = load_state()
    idx = index_price()
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    action, unrealized = "HOLD", 0.0
    pos = st["position"]

    # settle expired position (risk-reducing — never gated)
    if pos and now_ms >= pos["expiry_ms"]:
        intrinsic = (max(pos["kp"] - idx, 0) + max(idx - pos["kc"], 0)) * pos["mult"]
        pnl = pos["credit_total"] - intrinsic
        st["equity"] += pnl
        st["realized"] += pnl
        st["n_trades"] += 1
        st["wins"] += pnl > 0
        append(TRADES, {"ts": _now(), "action": "SETTLE", "pnl": round(pnl, 2),
                        "intrinsic": round(intrinsic, 2), "settle_index": idx,
                        "put": pos["put"], "call": pos["call"],
                        "credit_total": round(pos["credit_total"], 2)})
        st["position"] = pos = None
        action = "SETTLED"

    # open new strangle at real bids (SLA-gated)
    if pos is None:
        from data.quality.sla import preflight
        allowed, why = preflight(["deribit"], strategy="btc_wheel_delta")
        if not allowed:
            action += "+OPEN_BLOCKED_SLA"
            sel = None
        else:
            sel = pick_strangle_delta(idx)
        if sel is None:
            if "OPEN_BLOCKED_SLA" not in action:
                action += "+NO_LISTING"
        else:
            tp, tc = ticker(sel["put"]["name"]), ticker(sel["call"]["name"])
            bid_p = float(tp.get("best_bid_price") or 0) * idx
            bid_c = float(tc.get("best_bid_price") or 0) * idx
            if bid_p <= 0 or bid_c <= 0:
                action += "+NO_BID"
            else:
                mult = st["equity"] * NOTIONAL_PCT / idx
                fees = (leg_fee_usd(bid_p, idx) + leg_fee_usd(bid_c, idx)) * mult
                credit_total = (bid_p + bid_c) * mult - fees
                pos = {
                    "put": sel["put"]["name"], "call": sel["call"]["name"],
                    "kp": sel["put"]["k"], "kc": sel["call"]["k"],
                    "put_delta": round(sel["put"]["delta"], 3),
                    "call_delta": round(sel["call"]["delta"], 3),
                    "expiry_ms": sel["expiry_ms"], "mult": mult,
                    "entry_index": idx, "entry_bid_p": bid_p, "entry_bid_c": bid_c,
                    "credit_total": credit_total, "opened": _now(),
                }
                st["position"] = pos
                append(TRADES, {"ts": _now(), "action": "OPEN", **{
                    k: (round(v, 4) if isinstance(v, float) else v)
                    for k, v in pos.items()}})
                action = "OPENED" if action == "HOLD" else action + "+OPENED"

    # daily mark at real mark prices
    if pos:
        try:
            mp = float(ticker(pos["put"])["mark_price"]) * idx
            mc = float(ticker(pos["call"])["mark_price"]) * idx
            unrealized = pos["credit_total"] - (mp + mc) * pos["mult"]
        except Exception:
            unrealized = 0.0
            action += "+MARK_FAILED"

    append(DAILY, {
        "date": _now()[:10], "logged_at": _now(), "index": idx,
        "action": action, "position_open": pos is not None,
        "put": pos["put"] if pos else None, "call": pos["call"] if pos else None,
        "realized": round(st["realized"], 2),
        "unrealized": round(unrealized, 2),
        "total_equity": round(st["equity"] + unrealized, 2),
        "n_trades": st["n_trades"], "wins": st["wins"],
    })
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))
    print(f"[{_now()}] DELTA-WHEEL idx={idx:.0f} action={action} "
          f"equity={st['equity'] + unrealized:,.2f} "
          f"pos={pos['put'] + ' | ' + pos['call'] if pos else 'flat'}"
          + (f" (deltas {pos['put_delta']}/{pos['call_delta']})" if pos and 'put_delta' in pos else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="once", choices=["once"])
    ap.parse_args()
    run_once()
