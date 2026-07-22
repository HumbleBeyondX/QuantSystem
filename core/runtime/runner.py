"""PaperRunner — one process hosts N runtime strategies.

Per cadence tick for each strategy: rollover → manage exits → on_tick →
rails → fills → ledger. Crash isolation per strategy; halts on daily
loss cap enforced here (single implementation).

Run: python -u -m core.runtime.runner            (all v2 strategies)
     python -u -m core.runtime.runner --only scalp_trend_v2 [--once]
"""
from __future__ import annotations

import argparse
import time
from typing import Dict, List

from core.logging import get_logger
from core.runtime.context import LiveContext
from core.runtime.fills import MakerShadow, TakerShadow
from core.runtime.ledger import Ledger
from core.runtime.rails import RailsConfig, RiskRails
from core.runtime.strategy import Strategy

logger = get_logger(__name__)


def discover_strategies() -> Dict[str, type]:
    """v2 strategies self-register by import (strategies_v2 package)."""
    import strategies_v2
    return dict(strategies_v2.REGISTRY)


class StrategyHost:
    def __init__(self, strat: Strategy, ex, initial_equity: float,
                 rails_cfg: RailsConfig):
        self.strat = strat
        self.ledger = Ledger(strat.id, initial_equity)
        self.rails = RiskRails(rails_cfg)
        self.cfg = rails_cfg
        if getattr(strat, "engine", None) == "options_strangle":
            from core.runtime.options_fills import OptionsStrangleShadow
            engine_cls = OptionsStrangleShadow
        elif getattr(strat, "maker_only", False):
            engine_cls = MakerShadow
        else:
            engine_cls = TakerShadow
        self.engine = engine_cls(rails_cfg)
        self.ctx = LiveContext(ex, primary_tf=strat.primary_tf)
        self.st = self.ledger.load()
        self.next_due = 0.0

    def tick(self) -> None:
        st = self.st
        self.ledger.rollover(st)
        self.ctx.positions = st["positions"]
        self.ctx.equity = st["equity"]

        events = self.engine.manage(st["positions"], self.ctx)
        self.ledger.record_fills(st, events)

        # funding accrual hook (carry-type strategies): prorated per tick.
        # Short leg RECEIVES positive funding; long leg pays.
        if getattr(self.strat, "accrue_funding", False):
            frac = self.strat.cadence_sec / 28_800.0        # of an 8h period
            for sym, pos in st["positions"].items():
                f = self.ctx.funding(sym)
                if f is None:
                    continue
                bid, ask = self.ctx.book(sym)
                if not bid or not ask:
                    continue
                mark = (bid + ask) / 2
                d = 1 if pos["side"] == "long" else -1
                accr = -d * f * pos["qty"] * mark * frac
                st["equity"] += accr
                st["funding_pnl"] = round(st.get("funding_pnl", 0.0) + accr, 6)

        # daily cap check (single implementation for the whole fleet)
        cap = self.cfg.daily_loss_cap * self.ledger.initial_equity
        if st["realized_today"] <= -cap and not st["halted"]:
            st["halted"] = True
            logger.warning("daily_loss_cap_halt", strategy=self.strat.id,
                           realized=st["realized_today"])

        intents = self.strat.on_tick(self.ctx) or []
        approved, rejected = self.rails.check(
            intents, st["equity"], st["halted"],
            price_of=lambda s: (lambda b, a: (b + a) / 2 if b and a else None)
            (*self.ctx.book(s)))
        for r in rejected:
            logger.info("rails_rejected", strategy=self.strat.id, **r)
        events = self.engine.submit(approved, st["positions"],
                                    st.setdefault("quotes", {}), self.ctx)
        self.ledger.record_fills(st, events)

        # unrealized mark — engine-owned when it knows better (options
        # books mark at real option mark prices, not a perp bid/ask)
        if hasattr(self.engine, "unrealized"):
            unreal = self.engine.unrealized(st["positions"], self.ctx)
        else:
            unreal = 0.0
            for sym, pos in st["positions"].items():
                bid, ask = self.ctx.book(sym)
                if bid and ask:
                    d = 1 if pos["side"] == "long" else -1
                    mark = bid if d > 0 else ask
                    unreal += (mark - pos["entry"]) * d * pos["qty"]
        self.ledger.snapshot(st, unreal)
        self.ledger.save(st)
        for ev in events:
            logger.info("fill", strategy=self.strat.id, **{
                k: v for k, v in ev.items() if k != "ts"})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--equity", type=float, default=5_000.0)
    args = ap.parse_args()

    import ccxt
    ex = ccxt.okx({"options": {"defaultType": "swap"}, "timeout": 20000})
    ex.load_markets()

    hosts: List[StrategyHost] = []
    for sid, cls in discover_strategies().items():
        if args.only and sid not in args.only:
            continue
        strat = cls()
        rails = RailsConfig(**getattr(cls, "rails", {}))
        equity = getattr(cls, "initial_equity", args.equity)
        hosts.append(StrategyHost(strat, ex, equity, rails))
        logger.info("strategy_loaded", strategy=sid,
                    cadence=strat.cadence_sec,
                    engine=type(hosts[-1].engine).__name__)
    if not hosts:
        raise SystemExit("no strategies loaded")

    while True:
        now = time.time()
        for h in hosts:
            if now >= h.next_due:
                try:
                    h.tick()
                except Exception:
                    logger.exception("strategy_tick_failed",
                                     strategy=h.strat.id)
                h.next_due = now + h.strat.cadence_sec
        if args.once:
            break
        time.sleep(5)


if __name__ == "__main__":
    main()
