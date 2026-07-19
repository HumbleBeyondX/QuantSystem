"""Funding-carry pair — v2 runtime migration of funding_carry_runner.py.

Patience-trap harvester: scan the watchlist's funding each tick; when the
richest rate clears the threshold, maker-quote into a hedged pair
(short rich-funding leg + long hedge). Funding accrues via the runtime
hook (accrue_funding=True). Unwind maker on funding decay; the pair
stop is engine-side via per-leg stops far away (pair risk is small by
construction: equal notionals, ~90% divergence to liquidation).

Original scripts/funding_carry_runner.py keeps running untouched until
parity cutover.
"""
from __future__ import annotations

from typing import List

from core.runtime.intents import Intent
from core.runtime.strategy import ParamSpec, Strategy
from strategies_v2 import register


@register
class FundingCarryV2(Strategy):
    id = "funding_carry_v2"
    book_type = "carry"
    asset_domain = "tokenized"
    cadence_sec = 300
    universe = ["SPCX", "SKHYNIX", "NVDA", "TSLA", "HOOD", "MSTR", "CRCL",
                "WDC", "UVXY", "USO", "XLE", "URNM", "USAR"]
    primary_tf = "5m"
    maker_only = True
    accrue_funding = True
    rails = {"leverage": 20.0, "daily_loss_cap": 0.05,
             "max_margin_pct": 0.30}
    hedge_map = {"SKHYNIX": "NVDA", "MSTR": "CRCL", "NVDA": "TSLA"}
    default_hedge = "NVDA"

    params_schema = [
        ParamSpec("min_funding_bp", "float", 15.0, 5.0, 50.0, 1.0,
                  help="enter when richest funding >= this (bp per 8h)"),
        ParamSpec("exit_funding_bp", "float", 5.0, 0.0, 20.0, 1.0,
                  help="unwind after N consecutive reads below this"),
        ParamSpec("exit_reads", "int", 2, 1, 6, 1,
                  help="consecutive low-funding reads before unwind"),
        ParamSpec("leg_notional_usd", "float", 5000.0, 500.0, 20000.0, 500.0,
                  help="per-leg notional (equal both legs)"),
    ]

    def __init__(self, params=None):
        super().__init__(params)
        self._low_reads = 0

    def warmup_bars(self) -> int:
        return 2

    def _pair_symbols(self, ctx):
        """(short_leg, long_leg) of the open pair, if any."""
        shorts = [s for s, p in ctx.positions.items() if p["side"] == "short"]
        longs = [s for s, p in ctx.positions.items() if p["side"] == "long"]
        return (shorts[0] if shorts else None, longs[0] if longs else None)

    def on_tick(self, ctx) -> List[Intent]:
        intents: List[Intent] = []
        short_leg, long_leg = self._pair_symbols(ctx)

        if short_leg:                       # pair (or partial pair) open
            f = ctx.funding(short_leg)
            below = f is not None and f * 1e4 < self.p["exit_funding_bp"]
            self._low_reads = self._low_reads + 1 if below else 0
            if self._low_reads >= self.p["exit_reads"]:
                for sym in filter(None, (short_leg, long_leg)):
                    intents.append(Intent(action="close", symbol=sym,
                                          maker=True, tag="funding_gone"))
                self._low_reads = 0
            return intents

        # flat: scan for the richest funding
        best, best_f = None, 0.0
        for sym in self.universe:
            f = ctx.funding(sym)
            if f is not None and f > best_f:
                best, best_f = sym, f
        if best is None or best_f * 1e4 < self.p["min_funding_bp"]:
            return []
        hedge = self.hedge_map.get(best, self.default_hedge)
        if hedge == best:
            hedge = self.default_hedge if best != self.default_hedge else "TSLA"
        usd = self.p["leg_notional_usd"]
        intents.append(Intent(action="open", symbol=best, side="short",
                              maker=True, qty_usd=usd,
                              tag=f"carry_short_{best_f*1e4:.0f}bp"))
        intents.append(Intent(action="open", symbol=hedge, side="long",
                              maker=True, qty_usd=usd, tag="carry_hedge"))
        return intents
