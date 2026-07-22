"""RiskRails — the one place risk rules live (previously six copies).

Applied to every Intent BEFORE any fill engine sees it:
  * sizing: qty from risk_per_trade / stop distance when not given
  * stop clamp: stops forced inside the leverage liquidation distance
  * daily loss cap: opens blocked (reduce-only NEVER blocked — doctrine)
  * notional cap: margin per position <= max_margin_pct of equity

Rails config comes from the strategy's registry entry or defaults here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from core.logging import get_logger
from core.runtime.intents import Intent

logger = get_logger(__name__)


@dataclass
class RailsConfig:
    leverage: float = 20.0
    risk_per_trade: float = 0.01          # of sleeve equity
    daily_loss_cap: float = 0.03          # of sleeve equity; halt to next UTC day
    max_margin_pct: float = 0.30          # per position
    liq_buffer: float = 0.9               # stop must sit inside 90% of liq dist
    min_stop_pct: float = 0.0             # floor for stop distance (noise guard)
    maker_fee: float = 0.0002
    taker_fee: float = 0.0005


class RiskRails:
    def __init__(self, cfg: RailsConfig):
        self.cfg = cfg

    def check(self, intents: List[Intent], equity: float, halted: bool,
              price_of) -> Tuple[List[Intent], List[dict]]:
        """Filter + size intents. price_of(symbol) -> mid price."""
        ok: List[Intent] = []
        rejected: List[dict] = []
        for it in intents:
            if it.action in ("close", "cancel_quotes") or it.reduce_only:
                ok.append(it)                 # risk-reducing always passes
                continue
            if halted:
                rejected.append({"intent": it.symbol, "reason": "daily_cap_halted"})
                continue
            if it.meta.get("engine") == "options":
                # options strangles: sized by the strategy against its
                # notional_pct; no perp price/stop semantics apply. The
                # daily-cap halt above still blocks these opens.
                ok.append(it)
                continue
            px = it.price or price_of(it.symbol)
            if not px or px <= 0:
                rejected.append({"intent": it.symbol, "reason": "no_price"})
                continue

            if it.action == "open":
                liq_dist = px * self.cfg.liq_buffer / self.cfg.leverage
                stop = it.stop_dist if it.stop_dist else liq_dist
                stop = max(min(stop, liq_dist), px * self.cfg.min_stop_pct)
                it.stop_dist = stop
                if it.qty is None:
                    usd = it.qty_usd or (self.cfg.risk_per_trade * equity
                                         / (stop / px))
                    it.qty = usd / px
                margin = it.qty * px / self.cfg.leverage
                cap = self.cfg.max_margin_pct * equity
                if margin > cap:
                    it.qty = cap * self.cfg.leverage / px
            elif it.action == "quote" and it.qty is None:
                it.qty = (it.qty_usd or 500.0) / px
            ok.append(it)
        return ok, rejected
