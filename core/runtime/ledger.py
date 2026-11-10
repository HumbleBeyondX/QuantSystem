"""Ledger — unified state + telemetry, byte-compatible with the fleet.

Writes the EXACT jsonl/state shapes the platform already consumes
(monitor cards, universal risk providers, promotion evidence, watchdog
freshness): data/live_state/{sid}_state.json,
logs/paper_trading/{sid}_daily.jsonl and {sid}_trades.jsonl.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Dict, List

from core.paths import get_data_dir, get_logs_dir
from core.runtime.fills import FillEvent


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, sid: str, initial_equity: float):
        self.sid = sid
        self.initial_equity = initial_equity
        self.state_path = get_data_dir() / "live_state" / f"{sid}_state.json"
        self.daily_path = get_logs_dir() / "paper_trading" / f"{sid}_daily.jsonl"
        self.trades_path = get_logs_dir() / "paper_trading" / f"{sid}_trades.jsonl"

    # ── state ──
    def load(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {"equity": self.initial_equity, "realized_today": 0.0,
                "day": _now_iso()[:10], "halted": False,
                "positions": {}, "quotes": {}, "n_trades": 0, "wins": 0}

    def save(self, st: dict) -> None:
        # atomic write (tmp + rename): a crash mid-write must never leave a
        # truncated state file — same discipline as the data catalog
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(st, indent=1, default=str),
                       encoding="utf-8")
        os.replace(tmp, self.state_path)

    # ── events ──
    def record_fills(self, st: dict, events: List[FillEvent]) -> None:
        self.trades_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.trades_path, "a", encoding="utf-8") as f:
            for ev in events:
                if "pnl" in ev:
                    st["equity"] += ev["pnl"]
                    st["realized_today"] += ev["pnl"]
                    st["n_trades"] += 1
                    st["wins"] += ev["pnl"] > 0
                elif "fee" in ev:
                    st["equity"] -= ev["fee"]
                f.write(json.dumps(dict(ev)) + "\n")

    def snapshot(self, st: dict, unrealized: float = 0.0,
                 extra: Dict = None) -> None:
        row = {"ts": _now_iso(), "date": _now_iso()[:10],
               "logged_at": _now_iso(),
               "total_equity": round(st["equity"] + unrealized, 2),
               "realized_today": round(st["realized_today"], 2),
               "unrealized": round(unrealized, 2),
               "open_positions": list(st.get("positions", {})),
               "halted": st.get("halted", False),
               "n_trades": st.get("n_trades", 0),
               "wins": st.get("wins", 0), **(extra or {})}
        self.daily_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.daily_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def rollover(self, st: dict) -> None:
        today = _now_iso()[:10]
        if st.get("day") != today:
            st["day"], st["realized_today"], st["halted"] = today, 0.0, False
