"""OptionsStrangleShadow — runtime engine for short-strangle books.

Phase B shadow migrations (btc_wheel_v2 / btc_wheel_delta_v2): options
positions have an expiry LIFECYCLE, not stops — manage() settles at
intrinsic when expiry passes (index proxy, same documented approximation
as the originals). The strategy computes real-chain selections and
entry credits itself and hands the full strangle spec via Intent.meta;
the engine owns booking, marking and settlement.

Originals in scripts/ remain the promotion-evidence authority until
shadow parity + the 8-week window clear (red line).
"""
from __future__ import annotations

import json
import urllib.request
from typing import Dict, List

from core.runtime.fills import BaseFillEngine, FillEvent

DERIBIT = "https://www.deribit.com/api/v2/public"


def deribit_get(path: str) -> dict:
    with urllib.request.urlopen(f"{DERIBIT}/{path}", timeout=20) as r:
        return json.loads(r.read())["result"]


def btc_index() -> float:
    return float(deribit_get("get_index_price?index_name=btc_usd")["index_price"])


class OptionsStrangleShadow(BaseFillEngine):
    def manage(self, positions: Dict[str, dict], ctx) -> List[FillEvent]:
        events: List[FillEvent] = []
        now_ms = ctx.now().timestamp() * 1000
        for key, pos in list(positions.items()):
            if pos.get("kind") != "strangle":
                continue
            if now_ms >= pos["expiry_ms"]:
                idx = btc_index()
                intrinsic = (max(pos["kp"] - idx, 0)
                             + max(idx - pos["kc"], 0)) * pos["mult"]
                pnl = pos["credit_total"] - intrinsic
                events.append(FillEvent(
                    ts=ctx.now().isoformat(timespec="seconds"),
                    symbol=key, action="SETTLE",
                    price=idx, qty=pos["mult"], pnl=round(pnl, 2),
                    intrinsic=round(intrinsic, 2),
                    credit_total=round(pos["credit_total"], 2),
                    reason="expiry",
                    tag=f"{pos['put']}|{pos['call']}"))
                del positions[key]
        return events

    def submit(self, intents, positions, quotes, ctx) -> List[FillEvent]:
        events: List[FillEvent] = []
        for it in intents:
            if it.action != "open" or it.meta.get("kind") != "strangle":
                continue
            if it.symbol in positions:
                continue
            spec = dict(it.meta)
            spec["opened"] = ctx.now().isoformat(timespec="seconds")
            positions[it.symbol] = spec
            # NOTE: no "fee" key — credit_total is already net of fees and
            # equity only moves at settlement (pnl = credit - intrinsic),
            # exactly like the originals. A "fee" key here would double-count.
            events.append(FillEvent(
                ts=spec["opened"], symbol=it.symbol, action="OPEN",
                price=spec["entry_index"], qty=spec["mult"],
                fees_usd=round(spec.get("fees", 0.0), 4),
                tag=f"{spec['put']}|{spec['call']} credit={spec['credit_total']:.2f}"))
        return events

    def unrealized(self, positions: Dict[str, dict], ctx=None) -> float:
        total = 0.0
        for pos in positions.values():
            if pos.get("kind") != "strangle":
                continue
            try:
                idx = btc_index()
                mp = float(deribit_get(
                    f"ticker?instrument_name={pos['put']}")["mark_price"]) * idx
                mc = float(deribit_get(
                    f"ticker?instrument_name={pos['call']}")["mark_price"]) * idx
                total += pos["credit_total"] - (mp + mc) * pos["mult"]
            except Exception:
                continue
        return total
