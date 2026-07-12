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




# ===================================================================
# LIVE MODE — real Deribit orders. Gated by the promotion system:
# refuses to run unless data/stage_overrides.json says btc_wheel=live
# (set via POST /api/strategies/btc_wheel/go-live after gates pass).
#
# Safety design:
#   * DERIBIT_ENV=test (test.deribit.com) unless explicitly 'prod'.
#   * Sizing = paper notional x promotion throttle (default 10%).
#   * Separate state/logs (btc_wheel_live_*) — paper keeps running
#     in parallel as the control group.
#   * Sell limit at current bid (taker-equivalent, options spread ~1
#     tick); one re-quote on miss, then give up (no chasing).
#   * Settlement/marking stays risk-reducing: never blocked by SLA.
# ===================================================================

import os as _os

LIVE_STATE = ROOT / "data" / "live_state" / "btc_wheel_live_state.json"
LIVE_DAILY = ROOT / "logs" / "paper_trading" / "btc_wheel_live_daily.jsonl"
LIVE_TRADES = ROOT / "logs" / "paper_trading" / "btc_wheel_live_trades.jsonl"
LIVE_INITIAL_EQUITY = 10_000.0


def _live_base() -> str:
    env = _os.environ.get("DERIBIT_ENV", "test").lower()
    return ("https://www.deribit.com/api/v2" if env == "prod"
            else "https://test.deribit.com/api/v2")


def _load_env_file() -> None:
    envf = ROOT / ".env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                _os.environ.setdefault(k.strip(), v.strip())


class DeribitLive:
    """Minimal authed Deribit client (client_credentials grant)."""

    def __init__(self) -> None:
        _load_env_file()
        self.base = _live_base()
        cid = _os.environ.get("DERIBIT_CLIENT_ID")
        sec = _os.environ.get("DERIBIT_CLIENT_SECRET")
        if not cid or not sec:
            raise SystemExit(
                "live mode needs DERIBIT_CLIENT_ID / DERIBIT_CLIENT_SECRET in .env "
                f"(env={_os.environ.get('DERIBIT_ENV', 'test')} -> {self.base})")
        r = self._get("public/auth?grant_type=client_credentials"
                      f"&client_id={cid}&client_secret={sec}")
        self.token = r["access_token"]

    def _get(self, path: str, auth: bool = False) -> dict:
        req = urllib.request.Request(f"{self.base}/{path}")
        if auth:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            out = json.loads(resp.read())
        if "error" in out:
            raise RuntimeError(f"deribit error: {out['error']}")
        return out["result"]

    def sell_limit(self, instrument: str, amount: float, price: float) -> dict:
        q = (f"private/sell?instrument_name={instrument}&amount={amount}"
             f"&type=limit&price={price}&time_in_force=good_til_cancelled")
        return self._get(q, auth=True)

    def order_state(self, order_id: str) -> dict:
        return self._get(f"private/get_order_state?order_id={order_id}", auth=True)

    def cancel(self, order_id: str) -> dict:
        return self._get(f"private/cancel?order_id={order_id}", auth=True)

    def account_summary(self) -> dict:
        return self._get("private/get_account_summary?currency=BTC&extended=true",
                         auth=True)


def _promotion_allows_live():
    ov_path = ROOT / "data" / "stage_overrides.json"
    if not ov_path.exists():
        return False, 0.10
    try:
        ov = json.loads(ov_path.read_text()).get("btc_wheel", {})
        return ov.get("stage") == "live", float(ov.get("throttle", 0.10))
    except Exception:
        return False, 0.10


def _fill_leg(client, instr: str, amount: float):
    """Sell `amount` BTC of `instr` at bid; one re-quote; None if unfilled."""
    import time as _t
    for attempt in range(2):
        t = ticker(instr)
        bid = float(t.get("best_bid_price") or 0)
        if bid <= 0:
            return None
        res = client.sell_limit(instr, amount, bid)
        oid = res["order"]["order_id"]
        for _ in range(6):                      # ~30s poll
            _t.sleep(5)
            st = client.order_state(oid)
            if st["order_state"] == "filled":
                return {"order_id": oid, "price": float(st["average_price"]),
                        "amount": amount, "attempt": attempt + 1}
        try:
            client.cancel(oid)
        except Exception:
            pass
    return None


def run_live_once() -> None:
    allowed, throttle = _promotion_allows_live()
    if not allowed:
        raise SystemExit(
            "REFUSED: btc_wheel is not promoted to live. Pass the gates and "
            "POST /api/strategies/btc_wheel/go-live first (data/stage_overrides.json).")
    client = DeribitLive()
    print(f"[live] env={_os.environ.get('DERIBIT_ENV', 'test')} throttle={throttle:.0%}")

    st = (json.loads(LIVE_STATE.read_text()) if LIVE_STATE.exists()
          else {"equity": LIVE_INITIAL_EQUITY, "realized": 0.0, "position": None,
                "n_trades": 0, "wins": 0, "created": _now()})
    idx = index_price()
    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    action, unrealized = "HOLD", 0.0
    pos = st["position"]

    # settle (risk-reducing — never gated)
    if pos and now_ms >= pos["expiry_ms"]:
        kc = pos["kc"] if pos.get("call") else 0.0
        intrinsic = (max(pos["kp"] - idx, 0)
                     + (max(idx - kc, 0) if pos.get("call") else 0)) * pos["mult"]
        pnl = pos["credit_total"] - intrinsic
        st["equity"] += pnl
        st["realized"] += pnl
        st["n_trades"] += 1
        st["wins"] += pnl > 0
        append(LIVE_TRADES, {"ts": _now(), "action": "SETTLE", "pnl": round(pnl, 2),
                             "settle_index": idx, "put": pos["put"],
                             "call": pos.get("call")})
        st["position"] = pos = None
        action = "SETTLED"

    if pos is None:
        from data.quality.sla import preflight
        ok, why = preflight(["deribit"], strategy="btc_wheel_live")
        sel = pick_strangle(idx) if ok else None
        if not ok:
            action += "+OPEN_BLOCKED_SLA"
        elif sel is None:
            action += "+NO_LISTING"
        else:
            mult = st["equity"] * NOTIONAL_PCT * throttle / idx
            amount = max(round(mult, 1), 0.1)   # Deribit 0.1 BTC steps
            put_fill = _fill_leg(client, sel["put"]["name"], amount)
            call_fill = (_fill_leg(client, sel["call"]["name"], amount)
                         if put_fill else None)
            if put_fill and call_fill:
                credit = (put_fill["price"] + call_fill["price"]) * idx * amount
                fees = (leg_fee_usd(put_fill["price"] * idx, idx)
                        + leg_fee_usd(call_fill["price"] * idx, idx)) * amount
                pos = {"put": sel["put"]["name"], "call": sel["call"]["name"],
                       "kp": sel["put"]["k"], "kc": sel["call"]["k"],
                       "expiry_ms": sel["expiry_ms"], "mult": amount,
                       "entry_index": idx, "credit_total": credit - fees,
                       "opened": _now(),
                       "order_ids": [put_fill["order_id"], call_fill["order_id"]]}
                st["position"] = pos
                append(LIVE_TRADES, {"ts": _now(), "action": "OPEN", **{
                    k: (round(v, 4) if isinstance(v, float) else v)
                    for k, v in pos.items() if k != "order_ids"},
                    "order_ids": pos["order_ids"]})
                action = "OPENED" if action == "HOLD" else action + "+OPENED"
            elif put_fill and not call_fill:
                # one-legged: keep the short put (still short premium), flag loudly
                pos = {"put": sel["put"]["name"], "call": None,
                       "kp": sel["put"]["k"], "kc": 0.0,
                       "expiry_ms": sel["expiry_ms"], "mult": amount,
                       "entry_index": idx,
                       "credit_total": put_fill["price"] * idx * amount,
                       "opened": _now(), "order_ids": [put_fill["order_id"]]}
                st["position"] = pos
                append(LIVE_TRADES, {"ts": _now(), "action": "OPEN_PUT_ONLY",
                                     "detail": "call leg unfilled after requote"})
                action += "+CALL_LEG_UNFILLED"
            else:
                action += "+NO_FILL"

    if pos:
        try:
            mp = float(ticker(pos["put"])["mark_price"]) * idx if pos["put"] else 0
            mc = (float(ticker(pos["call"])["mark_price"]) * idx
                  if pos.get("call") else 0)
            unrealized = pos["credit_total"] - (mp + mc) * pos["mult"]
        except Exception:
            action += "+MARK_FAILED"

    acct_equity = None
    try:
        acct_equity = float(client.account_summary().get("equity", 0)) * idx
    except Exception:
        pass

    append(LIVE_DAILY, {
        "date": _now()[:10], "logged_at": _now(), "index": idx, "action": action,
        "position_open": pos is not None,
        "put": pos["put"] if pos else None,
        "call": (pos.get("call") if pos else None),
        "realized": round(st["realized"], 2), "unrealized": round(unrealized, 2),
        "total_equity": round(st["equity"] + unrealized, 2),
        "account_equity_usd": round(acct_equity, 2) if acct_equity else None,
        "n_trades": st["n_trades"], "wins": st["wins"],
    })
    LIVE_STATE.parent.mkdir(parents=True, exist_ok=True)
    LIVE_STATE.write_text(json.dumps(st, indent=1))
    print(f"[live {_now()}] idx={idx:.0f} action={action} "
          f"equity={st['equity'] + unrealized:,.2f} "
          f"pos={(pos['put'] + ' | ' + str(pos.get('call'))) if pos else 'flat'}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="once", choices=["once", "live"])
    args = ap.parse_args()
    if args.mode == "live":
        run_live_once()
    else:
        run_once()
