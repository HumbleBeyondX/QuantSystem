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
    """Quotes rest in `quotes` keyed by "{symbol}:{side}" — a symbol can
    carry a bid AND an ask simultaneously (market making). Fills check
    prev-bar extremes; opposite-side fills NET against the position
    (realizing pnl on the netted quantity) instead of being blocked."""

    @staticmethod
    def _qkey(symbol: str, side: str) -> str:
        return f"{symbol}:{side}"

    def _apply_fill(self, sym, side, px, qty, positions, ctx,
                    tag="", q=None):
        """Netting-aware fill: extends same-side, nets opposite-side."""
        ts = ctx.now().isoformat(timespec="seconds")
        fee = abs(px * qty) * self.cfg.maker_fee
        events = []
        pos = positions.get(sym)
        d_new = 1 if side == "long" else -1
        if pos is None:
            positions[sym] = {
                "side": side, "entry": px, "qty": qty,
                "stop_px": (px - d_new * q["stop_dist"]
                            if q and q.get("stop_dist") else None),
                "trail_dist": q.get("trail_dist") if q else None,
                "trail_activate": q.get("trail_activate") if q else None,
                "max_hold_ticks": q.get("max_hold_ticks") if q else None,
                "best": px, "ticks_held": 0, "tag": tag,
                "opened": ts, "entry_fee": fee,
            }
            events.append(FillEvent(ts=ts, symbol=sym,
                                    action=f"{side.upper()}_MAKER",
                                    price=px, qty=qty, fee=round(fee, 4),
                                    tag=tag))
        elif pos["side"] == side:                      # extend, avg entry
            tot = pos["qty"] + qty
            pos["entry"] = (pos["entry"] * pos["qty"] + px * qty) / tot
            pos["qty"] = tot
            events.append(FillEvent(ts=ts, symbol=sym,
                                    action=f"{side.upper()}_MAKER_ADD",
                                    price=px, qty=qty, fee=round(fee, 4),
                                    tag=tag))
        else:                                          # net against position
            d_pos = 1 if pos["side"] == "long" else -1
            netted = min(qty, pos["qty"])
            pnl = (px - pos["entry"]) * d_pos * netted - fee
            events.append(FillEvent(ts=ts, symbol=sym,
                                    action=f"NET_{pos['side'].upper()}",
                                    price=px, qty=netted,
                                    pnl=round(pnl, 4),
                                    reason=tag or "maker_net"))
            pos["qty"] -= netted
            if pos["qty"] <= 1e-12:
                del positions[sym]
                rem = qty - netted
                if rem > 1e-12:                        # flips through zero
                    positions[sym] = {
                        "side": side, "entry": px, "qty": rem,
                        "stop_px": None, "trail_dist": None,
                        "trail_activate": None, "max_hold_ticks": None,
                        "best": px, "ticks_held": 0, "tag": tag,
                        "opened": ts, "entry_fee": 0.0,
                    }
        return events

    def submit(self, intents, positions, quotes, ctx):
        events = []
        # 1. fill checks on resting quotes
        for key, q in list(quotes.items()):
            sym = q["symbol"]
            hi, lo = ctx.prev_bar_extremes(sym)
            filled = ((q["side"] == "long" and lo < q["price"])
                      or (q["side"] == "short" and hi > q["price"]))
            q["ticks"] = q.get("ticks", 0) + 1
            if filled:
                events.extend(self._apply_fill(
                    sym, q["side"], q["price"], q["qty"], positions, ctx,
                    tag=q.get("tag", ""), q=q))
                del quotes[key]
            elif q.get("ttl_ticks") and q["ticks"] >= q["ttl_ticks"]:
                del quotes[key]

        # 2. new intents
        for it in intents:
            if it.action == "cancel_quotes":
                for key in [k for k, q in quotes.items()
                            if q["symbol"] == it.symbol]:
                    del quotes[key]
                continue
            if not it.maker or it.action not in ("open", "quote", "close"):
                continue
            side = it.side
            if it.action == "close":
                pos = positions.get(it.symbol)
                if not pos:
                    continue
                side = "short" if pos["side"] == "long" else "long"
                it.qty = it.qty or pos["qty"]
            bid, ask = ctx.book(it.symbol)
            px = it.price or (bid if side == "long" else ask)
            if not px or not it.qty:
                continue
            quotes[self._qkey(it.symbol, side)] = {
                "symbol": it.symbol, "side": side, "price": px,
                "qty": it.qty, "stop_dist": it.stop_dist,
                "trail_dist": it.trail_dist,
                "trail_activate": it.trail_activate,
                "max_hold_ticks": it.max_hold_ticks,
                "ticks": 0, "ttl_ticks": it.meta.get("ttl_ticks", 12),
                "tag": it.tag,
            }
        return events
