#!/usr/bin/env python3
"""Tokenized-stock 20x trend scalp — LIVE shadow runner (owner sleeve).

Owner mandate: opportunistic sleeve, gate waived, risk rails instead.
This process trades VIRTUALLY in real time so parameters can be
iterated same-day; flipping to real orders later reuses the same signal
loop with the ccxt execution engine.

Loop (every 5m bar close):
  * fetch last ~200 x 5m bars per symbol (direct OKX, no collector dep)
  * signals: Donchian(10) breakout / EMA(21) pullback (same machinery as
    the mechanics test — scripts/backtest_btc_sniper.compute_signals)
  * entries long+short, stop = min(ATR_STOP x ATR, 0.9 x liq distance)
  * trail = ATR_TRAIL x ATR after ATR_ACT profit; time exit MAX_HOLD bars
  * shadow fills at current best bid/ask (real book)
Risk rails (hard):
  * SLEEVE equity 5,000 USDT virtual, RISK 1% per trade
  * daily loss cap 3% of sleeve -> flat + halt until next UTC day
State:  data/live_state/scalp_sleeve_state.json
Logs:   logs/paper_trading/scalp_sleeve_daily.jsonl / _trades.jsonl
Run:    python -u scripts/scalp_paper_runner.py            (Ctrl+C stops)
        python -u scripts/scalp_paper_runner.py --once     (single pass)
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

# ── owner-tunable knobs (iterate freely) ─────────────────────────
UNIVERSE = ["SPCX", "NVDA", "TSLA", "HOOD"]
LEV = 20.0
SLEEVE_EQUITY = 5_000.0
RISK_PER_TRADE = 0.01          # 1% of sleeve
DAILY_LOSS_CAP = 0.03          # halt for the day at -3%
ATR_STOP = 1.5                 # x ATR (clamped inside liq anyway)
ATR_TRAIL = 1.0
ATR_ACT = 0.5                  # trail activates after this many ATR profit
MAX_HOLD_BARS = 72             # 6h
MIN_SCORE = 2
FEE = 0.0005
LIQ_BUFFER = 0.9

STATE = ROOT / "data" / "live_state" / "scalp_sleeve_state.json"
DAILY = ROOT / "logs" / "paper_trading" / "scalp_sleeve_daily.jsonl"
TRADES = ROOT / "logs" / "paper_trading" / "scalp_sleeve_trades.jsonl"


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
    return {"equity": SLEEVE_EQUITY, "realized_today": 0.0,
            "day": now_iso()[:10], "halted": False,
            "positions": {}, "n_trades": 0, "wins": 0}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, indent=1))


def run_pass(ex, st: dict, sig_fn) -> None:
    import pandas as pd

    # daily rollover / halt
    today = now_iso()[:10]
    if st["day"] != today:
        st["day"], st["realized_today"], st["halted"] = today, 0.0, False
    if st["realized_today"] <= -DAILY_LOSS_CAP * SLEEVE_EQUITY:
        if not st["halted"]:
            say(f"DAILY LOSS CAP HIT ({st['realized_today']:+.0f}) — halted until next UTC day")
            st["halted"] = True

    for base in UNIVERSE:
        sym = f"{base}/USDT:USDT"
        try:
            ohlcv = ex.fetch_ohlcv(sym, "5m", limit=200)
            if len(ohlcv) < 60:
                continue
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high",
                                              "low", "close", "volume"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
            sig = sig_fn(df.copy())
            row = sig.iloc[-2]          # last CLOSED bar
            live = sig.iloc[-1]
            atr = float(row.get("atr") or 0)
            px_now = float(live["close"])
            ob = ex.fetch_order_book(sym, limit=5)
            bid = ob["bids"][0][0] if ob["bids"] else px_now
            ask = ob["asks"][0][0] if ob["asks"] else px_now

            pos = st["positions"].get(base)

            # ── manage open position ──
            if pos:
                pos["bars_held"] += 1
                d = pos["dir"]
                mark = bid if d > 0 else ask
                pos["best"] = max(pos["best"], mark) if d > 0 else min(pos["best"], mark)
                profit_atr = (mark - pos["entry"]) * d / max(pos["atr"], 1e-9)
                trail_on = profit_atr > ATR_ACT
                trail_px = (pos["best"] - ATR_TRAIL * pos["atr"] if d > 0
                            else pos["best"] + ATR_TRAIL * pos["atr"])
                reason = None
                if (d > 0 and mark <= pos["stop"]) or (d < 0 and mark >= pos["stop"]):
                    reason, exit_px = "stop", pos["stop"]
                elif trail_on and ((d > 0 and mark <= trail_px)
                                   or (d < 0 and mark >= trail_px)):
                    reason, exit_px = "trail", trail_px
                elif pos["bars_held"] >= MAX_HOLD_BARS:
                    reason, exit_px = "time", mark
                if reason:
                    pnl = (exit_px - pos["entry"]) * d * pos["qty"] \
                        - abs(exit_px * pos["qty"]) * FEE
                    st["equity"] += pnl
                    st["realized_today"] += pnl
                    st["n_trades"] += 1
                    st["wins"] += pnl > 0
                    del st["positions"][base]
                    append(TRADES, {"ts": now_iso(), "symbol": base,
                                    "action": f"CLOSE_{'L' if d > 0 else 'S'}",
                                    "reason": reason, "entry": pos["entry"],
                                    "exit": round(exit_px, 4),
                                    "pnl": round(pnl, 2),
                                    "bars": pos["bars_held"]})
                    say(f"{base}: CLOSE {reason} pnl={pnl:+.2f} "
                        f"(day {st['realized_today']:+.0f}, eq {st['equity']:.0f})")
                    continue

            # ── entries (blocked when halted) ──
            if not pos and not st["halted"] and atr > 0:
                score_l = int(bool(row.get("breakout_up", 0))) \
                    + int(bool(row.get("near_ema21_up", 0))) \
                    + int(float(row.get("vol_ratio", 0) or 0) >= 1.2)
                score_s = int(bool(row.get("breakout_dn", 0))) \
                    + int(bool(row.get("near_ema21_dn", 0))) \
                    + int(float(row.get("vol_ratio", 0) or 0) >= 1.2)
                d = 1 if score_l >= MIN_SCORE else (-1 if score_s >= MIN_SCORE else 0)
                if d != 0:
                    entry = ask if d > 0 else bid          # cross the spread
                    liq_dist = entry * LIQ_BUFFER / LEV
                    stop_dist = min(ATR_STOP * atr, liq_dist)
                    qty = RISK_PER_TRADE * SLEEVE_EQUITY / stop_dist
                    margin = qty * entry / LEV
                    if margin > 0.3 * st["equity"]:
                        qty = 0.3 * st["equity"] * LEV / entry
                        stop_dist = min(stop_dist, liq_dist)
                    fee = abs(qty * entry) * FEE
                    st["equity"] -= fee
                    st["positions"][base] = {
                        "dir": d, "entry": entry, "qty": qty, "atr": atr,
                        "stop": entry - d * stop_dist, "best": entry,
                        "bars_held": 0, "opened": now_iso(),
                    }
                    append(TRADES, {"ts": now_iso(), "symbol": base,
                                    "action": "LONG" if d > 0 else "SHORT",
                                    "entry": round(entry, 4),
                                    "stop": round(entry - d * stop_dist, 4),
                                    "qty": round(qty, 4),
                                    "stop_pct": round(stop_dist / entry * 100, 2)})
                    say(f"{base}: {'LONG' if d > 0 else 'SHORT'} @ {entry:.4f} "
                        f"stop {stop_dist/entry*100:.2f}% qty {qty:.3f}")
        except Exception as e:
            say(f"{base}: pass failed {type(e).__name__}: {str(e)[:60]}")

    append(DAILY, {"ts": now_iso(), "equity": round(st["equity"], 2),
                   "total_equity": round(st["equity"], 2),
                   "logged_at": now_iso(), "date": now_iso()[:10],
                   "realized_today": round(st["realized_today"], 2),
                   "open_positions": list(st["positions"]),
                   "halted": st["halted"], "n_trades": st["n_trades"],
                   "wins": st["wins"]})
    save_state(st)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    say("importing (ccxt/pandas/signals)...")
    import ccxt
    from scripts.backtest_btc_sniper import compute_signals
    ex = ccxt.okx({"options": {"defaultType": "swap"}, "timeout": 20000})
    ex.load_markets()
    say(f"runner up: {UNIVERSE} lev={LEV}x risk={RISK_PER_TRADE:.0%}/trade "
        f"daily_cap={DAILY_LOSS_CAP:.0%} sleeve=${SLEEVE_EQUITY:,.0f} (VIRTUAL)")

    st = load_state()
    while True:
        run_pass(ex, st, compute_signals)
        if args.once:
            break
        # sleep to next 5m boundary + 5s
        wait = 300 - (time.time() % 300) + 5
        time.sleep(wait)


if __name__ == "__main__":
    main()
