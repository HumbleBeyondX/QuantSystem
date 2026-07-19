"""FillEngines — turn approved Intents into fills; manage open positions.

TakerShadow : immediate virtual fill crossing the spread at the book.
MakerShadow : posts quotes; a quote fills only when the market trades
              THROUGH it (prev-bar-through rule — conservative direction,
              queue position ignored; documented v0 caveat).

Both engines ALSO own generic position management each tick: stop-loss,
trailing stop, max-hold — mechanics identical in backtest and paper.
The future live executor implements this same interface with real
post-only/market orders; strategies never know the difference.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from core.runtime.intents import Intent
from core.runtime.rails import RailsConfig


class FillEvent(dict):
    """dict with keys: ts, symbol, action, price, qty, pnl?, reason?, tag?"""


class BaseFillEngine:
    def __init__(self, cfg: RailsConfig):
        self.cfg = cfg

    # ── position management (generic exits) ──
    def manage(self, positions: Dict[str, dict], ctx) -> List[FillEvent]:
        events: List[FillEvent] = []
        for sym, pos in list(positions.items()):
            bid, ask = ctx.book(sym)
            if not bid or not ask:
                continue
            d = 1 if pos["side"] == "long" else -1
            mark = bid if d > 0 else ask
            pos["ticks_held"] = pos.get("ticks_held", 0) + 1
            pos["best"] = (max(pos.get("best", mark), mark) if d > 0
                           else min(pos.get("best", mark), mark))
            reason = exit_px = None
            stop_px = pos.get("stop_px")
            if stop_px and ((d > 0 and mark <= stop_px)
                            or (d < 0 and mark >= stop_px)):
                reason, exit_px = "stop", stop_px
            elif pos.get("trail_dist"):
                armed = (abs(pos["best"] - pos["entry"])
                         >= (pos.get("trail_activate") or 0))
                trail_px = (pos["best"] - pos["trail_dist"] if d > 0
                            else pos["best"] + pos["trail_dist"])
                if armed and ((d > 0 and mark <= trail_px)
                              or (d < 0 and mark >= trail_px)):
                    reason, exit_px = "trail", trail_px
            if reason is None and pos.get("max_hold_ticks") \
                    and pos["ticks_held"] >= pos["max_hold_ticks"]:
                reason, exit_px = "time", mark
            if reason:
                events.append(self._close(sym, pos, exit_px, reason, ctx))
                del positions[sym]
        return events

    def _close(self, sym: str, pos: dict, px: float, reason: str,
               ctx) -> FillEvent:
        d = 1 if pos["side"] == "long" else -1
        fee = abs(px * pos["qty"]) * self.cfg.taker_fee
        pnl = (px - pos["entry"]) * d * pos["qty"] - fee
        return FillEvent(ts=ctx.now().isoformat(timespec="seconds"),
                         symbol=sym, action=f"CLOSE_{pos['side'].upper()}",
                         price=round(px, 6), qty=pos["qty"],
                         pnl=round(pnl, 4), reason=reason,
                         tag=pos.get("tag", ""))

    def submit(self, intents: List[Intent], positions: Dict[str, dict],
               quotes: Dict[str, dict], ctx) -> List[FillEvent]:
        raise NotImplementedError


class TakerShadow(BaseFillEngine):
    def submit(self, intents, positions, quotes, ctx) -> List[FillEvent]:
        events: List[FillEvent] = []
        for it in intents:
            bid, ask = ctx.book(it.symbol)
            if not bid or not ask:
                continue
            if it.action == "open" and it.symbol not in positions:
                px = ask if it.side == "long" else bid
                fee = abs(px * it.qty) * self.cfg.taker_fee
                d = 1 if it.side == "long" else -1
                positions[it.symbol] = {
                    "side": it.side, "entry": px, "qty": it.qty,
                    "stop_px": px - d * it.stop_dist if it.stop_dist else None,
                    "trail_dist": it.trail_dist,
                    "trail_activate": it.trail_activate,
                    "max_hold_ticks": it.max_hold_ticks,
                    "best": px, "ticks_held": 0, "tag": it.tag,
                    "opened": ctx.now().isoformat(timespec="seconds"),
                    "entry_fee": fee,
                }
                events.append(FillEvent(
                    ts=ctx.now().isoformat(timespec="seconds"),
                    symbol=it.symbol, action=it.side.upper(),
                    price=round(px, 6), qty=it.qty, fee=round(fee, 4),
                    tag=it.tag))
            elif it.action == "close" and it.symbol in positions:
                pos = positions.pop(it.symbol)
                px = bid if pos["side"] == "long" else ask
                events.append(self._close(it.symbol, pos, px,
                                          it.tag or "signal", ctx))
        return events


class MakerShadow(BaseFillEngine):
    """Quotes rest in `quotes`; fills checked against prev-bar extremes."""

    def submit(self, intents, positions, quotes, ctx) -> List[FillEvent]:
        events: List[FillEvent] = []
        # 1. fill checks on resting quotes
        for sym, q in list(quotes.items()):
            hi, lo = ctx.prev_bar_extremes(sym)
            filled = ((q["side"] in ("long",) and lo < q["price"])
                      or (q["side"] == "short" and hi > q["price"]))
            q["ticks"] = q.get("ticks", 0) + 1
            if filled:
                px = q["price"]
                fee = abs(px * q["qty"]) * self.cfg.maker_fee
                if q.get("closing") and sym in positions:
                    pos = positions.pop(sym)
                    d = 1 if pos["side"] == "long" else -1
                    pnl = (px - pos["entry"]) * d * pos["qty"] - fee
                    events.append(FillEvent(
                        ts=ctx.now().isoformat(timespec="seconds"), symbol=sym,
                        action=f"CLOSE_{pos['side'].upper()}", price=px,
                        qty=pos["qty"], pnl=round(pnl, 4),
                        reason=q.get("tag") or "maker_exit"))
                elif not q.get("closing"):
                    d = 1 if q["side"] == "long" else -1
                    positions[sym] = {
                        "side": q["side"], "entry": px, "qty": q["qty"],
                        "stop_px": px - d * q["stop_dist"] if q.get("stop_dist") else None,
                        "trail_dist": q.get("trail_dist"),
                        "trail_activate": q.get("trail_activate"),
                        "max_hold_ticks": q.get("max_hold_ticks"),
                        "best": px, "ticks_held": 0, "tag": q.get("tag", ""),
                        "opened": ctx.now().isoformat(timespec="seconds"),
                        "entry_fee": fee,
                    }
                    events.append(FillEvent(
                        ts=ctx.now().isoformat(timespec="seconds"), symbol=sym,
                        action=f"{q['side'].upper()}_MAKER", price=px,
                        qty=q["qty"], fee=round(fee, 4), tag=q.get("tag", "")))
                del quotes[sym]
            elif q.get("ttl_ticks") and q["ticks"] >= q["ttl_ticks"]:
                del quotes[sym]                  # expire unfilled quote

        # 2. new intents
        for it in intents:
            if it.action == "cancel_quotes":
                quotes.pop(it.symbol, None)
            elif it.action in ("open", "quote", "close") and it.maker:
                if it.action == "open" and it.symbol in positions:
                    continue
                bid, ask = ctx.book(it.symbol)
                px = it.price or (bid if it.side == "long" else ask)
                if not px:
                    continue
                quotes[it.symbol] = {
                    "side": it.side or ("short" if it.action == "close"
                                        and positions.get(it.symbol, {})
                                        .get("side") == "long" else it.side),
                    "price": px, "qty": it.qty,
                    "stop_dist": it.stop_dist, "trail_dist": it.trail_dist,
                    "trail_activate": it.trail_activate,
                    "max_hold_ticks": it.max_hold_ticks,
                    "closing": it.action == "close", "ticks": 0,
                    "ttl_ticks": it.meta.get("ttl_ticks", 12),
                    "tag": it.tag,
                }
        return events
