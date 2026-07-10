#!/usr/bin/env python3
"""BTC Wheel paper trader — REAL Deribit chains, shadow execution.

Strategy (validated in backtest_combo_wheel_sniper.py: wheel leg Sharpe 1.35,
combo w/ ETH Sniper OOS 2.40):
  Sell weekly BTC strangle: put @ ~0.92×index, call @ ~1.08×index,
  nearest Friday expiry 5-9 days out. Hold to expiry, settle, roll.
  Notional = 50% of paper equity.

Execution realism (user rule: real option chains, no synthesis):
  * Instruments/strikes: REAL live Deribit listings (public API, no auth).
  * Entry fills at the real BID of each leg (conservative for a seller).
  * Daily MTM at real MARK prices. Prices are BTC-denominated → ×index.
  * Fees: Deribit options taker 0.0003 BTC/contract, capped at 12.5% premium.
  * Settlement: intrinsic at first run past expiry (index proxy for
    delivery price — logged with settle_index for audit).

State:  data/live_state/btc_wheel_state.json      (human-readable)
Logs:   logs/paper_trading/btc_wheel_daily.jsonl  (monitor feed)
        logs/paper_trading/btc_wheel_trades.jsonl
Run:    python scripts/wheel_paper_trade.py --mode once   (daily cron)
"""
from __future__ import annotations
import argparse, json, sys, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STATE = ROOT / "data" / "live_state" / "btc_wheel_state.json"
DAILY = ROOT / "logs" / "paper_trading" / "btc_wheel_daily.jsonl"
TRADES = ROOT / "logs" / "paper_trading" / "btc_wheel_trades.jsonl"
API = "https://www.deribit.com/api/v2/public"

PUT_K, CALL_K = 0.92, 1.08
NOTIONAL_PCT = 0.50
INITIAL_EQUITY = 100_000.0
FEE_BTC = 0.0003          # per contract, capped at 12.5% of premium


def get(path: str) -> dict:
    with urllib.request.urlopen(f"{API}/{path}", timeout=30) as r:
        return json.loads(r.read())["result"]


def index_price() -> float:
    return float(get("get_index_price?index_name=btc_usd")["index_price"])


def ticker(instr: str) -> dict:
    return get(f"ticker?instrument_name={instr}")


def pick_strangle(idx: float) -> dict | None:
    """Nearest-Friday 5-9 DTE expiry; strikes nearest 0.92/1.08 × index."""
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
    puts = [r for r in cand if r["exp"] == exp and r["cp"] == "put"]
    calls = [r for r in cand if r["exp"] == exp and r["cp"] == "call"]
    if not puts or not calls:
        return None
    p = min(puts, key=lambda r: abs(r["k"] - idx * PUT_K))
    c = min(calls, key=lambda r: abs(r["k"] - idx * CALL_K))
    return {"put": p, "call": c, "expiry_ms": exp}


def leg_fee_usd(prem_usd: float, idx: float) -> float:
    return min(FEE_BTC * idx, 0.125 * prem_usd)


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"equity": INITIAL_EQUITY, "realized": 0.0, "position": None,
            "n_trades": 0, "wins": 0, "created": _now()}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(row) + "\n")


def run_once() -> None:
    st = load_state()
    idx = index_price()
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    action, unrealized = "HOLD", 0.0
    pos = st["position"]

    # ── settle expired position ──
    if pos and now_ms >= pos["expiry_ms"]:
        intrinsic = (max(pos["kp"] - idx, 0) + max(idx - pos["kc"], 0)) * pos["mult"]
        pnl = pos["credit_total"] - intrinsic
        st["equity"] += pnl
        st["realized"] += pnl
        st["n_trades"] += 1
        st["wins"] += pnl > 0
        append(TRADES, {
            "ts": _now(), "action": "SETTLE", "pnl": round(pnl, 2),
            "intrinsic": round(intrinsic, 2), "settle_index": idx,
            "put": pos["put"], "call": pos["call"],
            "credit_total": round(pos["credit_total"], 2),
        })
        st["position"] = pos = None
        action = "SETTLED"

    # ── open new strangle at real bids ──
    if pos is None:
        # SLA preflight: risk-increasing action only. A degraded Deribit
        # blocks OPENING; settlement/marking above always proceeded.
        from data.quality.sla import preflight
        allowed, why = preflight(["deribit"], strategy="btc_wheel")
        if not allowed:
            action += "+OPEN_BLOCKED_SLA"
            sel = None
        else:
            sel = pick_strangle(idx)
        if sel is None:
            if "OPEN_BLOCKED_SLA" not in action:
                action += "+NO_LISTING"
        else:
            tp, tc = ticker(sel["put"]["name"]), ticker(sel["call"]["name"])
            bid_p = float(tp.get("best_bid_price") or 0) * idx
            bid_c = float(tc.get("best_bid_price") or 0) * idx
            if bid_p <= 0 or bid_c <= 0:
                action += "+NO_BID"       # refuse to fill on empty book
            else:
                mult = st["equity"] * NOTIONAL_PCT / idx
                fees = (leg_fee_usd(bid_p, idx) + leg_fee_usd(bid_c, idx)) * mult
                credit_total = (bid_p + bid_c) * mult - fees
                pos = {
                    "put": sel["put"]["name"], "call": sel["call"]["name"],
                    "kp": sel["put"]["k"], "kc": sel["call"]["k"],
                    "expiry_ms": sel["expiry_ms"], "mult": mult,
                    "entry_index": idx, "entry_bid_p": bid_p, "entry_bid_c": bid_c,
                    "credit_total": credit_total, "opened": _now(),
                }
                st["position"] = pos
                append(TRADES, {"ts": _now(), "action": "OPEN", **{
                    k: (round(v, 4) if isinstance(v, float) else v)
                    for k, v in pos.items()}})
                action = "OPENED" if action == "HOLD" else action + "+OPENED"

    # ── daily mark at real mark prices ──
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
    save_state(st)
    print(f"[{_now()}] idx={idx:.0f} action={action} "
          f"equity={st['equity'] + unrealized:,.2f} "
          f"(realized {st['realized']:+,.2f} / unreal {unrealized:+,.2f}) "
          f"pos={pos['put'] + ' | ' + pos['call'] if pos else 'flat'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="once", choices=["once"])
    ap.parse_args()
    run_once()
