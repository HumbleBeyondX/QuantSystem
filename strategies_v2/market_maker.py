"""Two-sided market maker — v2 runtime migration of mm_paper_runner.py.

Join the touch on wide-spread tokenized ETF perps, inventory-skewed,
netting handled by the MakerShadow engine. Original mm_paper_runner.py
keeps running untouched until parity cutover.
"""
from __future__ import annotations

from typing import List

from core.runtime.intents import Intent
from core.runtime.strategy import ParamSpec, Strategy
from strategies_v2 import register


@register
class MarketMakerV2(Strategy):
    id = "mm_v2"
    book_type = "mm"
    asset_domain = "tokenized"
    cadence_sec = 60
    universe = ["XLE", "URNM", "UVXY", "USO"]
    primary_tf = "1m"
    maker_only = True
    rails = {"leverage": 20.0, "daily_loss_cap": 0.03,
             "max_margin_pct": 0.50}

    params_schema = [
        ParamSpec("quote_usd", "float", 500.0, 100.0, 2000.0, 100.0,
                  help="quote size per side"),
        ParamSpec("inv_cap_units", "int", 3, 1, 10, 1,
                  help="stop quoting the loaded side beyond this inventory"),
        ParamSpec("skew_bps_per_unit", "float", 2.0, 0.0, 10.0, 0.5,
                  help="shift both quotes against inventory"),
        ParamSpec("quote_ttl_ticks", "int", 5, 1, 30, 1,
                  help="requote after this many ticks unfilled"),
    ]

    def warmup_bars(self) -> int:
        return 2

    def on_tick(self, ctx) -> List[Intent]:
        intents: List[Intent] = []
        for sym in self.universe:
            bid, ask = ctx.book(sym)
            if not bid or not ask:
                continue
            mid = (bid + ask) / 2
            pos = ctx.position(sym)
            inv_usd = 0.0
            if pos:
                inv_usd = (1 if pos["side"] == "long" else -1) \
                    * pos["qty"] * mid
            inv_units = inv_usd / self.p["quote_usd"]
            skew = mid * self.p["skew_bps_per_unit"] / 1e4 * inv_units

            intents.append(Intent(action="cancel_quotes", symbol=sym))
            qty = self.p["quote_usd"] / mid
            if inv_units < self.p["inv_cap_units"]:
                intents.append(Intent(
                    action="quote", symbol=sym, side="long",
                    price=round(bid - skew, 6), qty=qty, maker=True,
                    tag="mm_bid", meta={"ttl_ticks": self.p["quote_ttl_ticks"]}))
            if inv_units > -self.p["inv_cap_units"]:
                intents.append(Intent(
                    action="quote", symbol=sym, side="short",
                    price=round(ask - skew, 6), qty=qty, maker=True,
                    tag="mm_ask", meta={"ttl_ticks": self.p["quote_ttl_ticks"]}))
        return intents
