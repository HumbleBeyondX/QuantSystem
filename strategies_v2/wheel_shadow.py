"""Phase B shadow migrations: btc_wheel_v2 / btc_wheel_delta_v2.

These are runtime-v2 SHADOW copies of the two original wheel paper
traders. Parity by construction: strike selection, fee model and credit
math are IMPORTED from the original scripts (scripts/wheel_paper_trade,
scripts/wheel_delta_paper_trade) — not reimplemented. The runtime owns
booking/settlement via OptionsStrangleShadow.

Separate ledgers: data/live_state/btc_wheel_v2_state.json,
logs/paper_trading/btc_wheel_v2_daily.jsonl (and _delta_ variants).

RED LINES (Phase B doctrine, 2026-07):
  * The originals stay untouched and remain the SOLE promotion evidence
    until the 8-week window ends (~early Sept) AND parity diffs are clean.
  * No cutover inside the window; cutover only at a flat point.
  * Daily parity diff: scripts/wheel_parity_check.py.
"""
from __future__ import annotations

from typing import List

from core.runtime.intents import Intent
from core.runtime.strategy import ParamSpec, Strategy
from strategies_v2 import register

SYMBOL = "BTC-STRANGLE"


class _WheelShadowBase(Strategy):
    book_type = "options"
    asset_domain = "btc_options"
    cadence_sec = 3600            # hourly marks; decisions only when flat
    universe = [SYMBOL]
    primary_tf = "1d"
    engine = "options_strangle"
    initial_equity = 100_000.0
    rails = {"daily_loss_cap": 0.10}   # options book: settle swings > 3%
    params_schema = [
        ParamSpec("notional_pct", "float", 0.50, 0.1, 1.0, 0.05,
                  help="strangle notional as fraction of sleeve equity"),
    ]

    def _select(self, idx: float) -> dict | None:
        raise NotImplementedError

    def on_tick(self, ctx) -> List[Intent]:
        if ctx.positions.get(SYMBOL):
            return []                       # engine settles at expiry

        # SLA preflight — risk-increasing action only (same as originals)
        from data.quality.sla import preflight
        allowed, _why = preflight(["deribit"], strategy=self.id)
        if not allowed:
            return []

        from scripts.wheel_paper_trade import (index_price, leg_fee_usd,
                                               ticker)
        idx = index_price()
        sel = self._select(idx)
        if sel is None:
            return []
        tp, tc = ticker(sel["put"]["name"]), ticker(sel["call"]["name"])
        bid_p = float(tp.get("best_bid_price") or 0) * idx
        bid_c = float(tc.get("best_bid_price") or 0) * idx
        if bid_p <= 0 or bid_c <= 0:
            return []                       # refuse to fill on empty book
        mult = ctx.equity * self.p["notional_pct"] / idx
        fees = (leg_fee_usd(bid_p, idx) + leg_fee_usd(bid_c, idx)) * mult
        credit_total = (bid_p + bid_c) * mult - fees
        return [Intent(
            action="open", symbol=SYMBOL, side="short", qty=mult,
            tag=f"{sel['put']['name']}|{sel['call']['name']}",
            meta={"kind": "strangle", "engine": "options",
                  "put": sel["put"]["name"], "call": sel["call"]["name"],
                  "kp": sel["put"]["k"], "kc": sel["call"]["k"],
                  "expiry_ms": sel["expiry_ms"], "mult": mult,
                  "entry_index": idx,
                  "entry_bid_p": round(bid_p, 4),
                  "entry_bid_c": round(bid_c, 4),
                  "fees": round(fees, 4),
                  "credit_total": round(credit_total, 4)})]

    def warmup_bars(self) -> int:
        return 0


@register
class BtcWheelShadowV2(_WheelShadowBase):
    """Fixed-strike wheel shadow (original: scripts/wheel_paper_trade.py)."""
    id = "btc_wheel_v2"

    def _select(self, idx: float) -> dict | None:
        from scripts.wheel_paper_trade import pick_strangle
        return pick_strangle(idx)


@register
class BtcWheelDeltaShadowV2(_WheelShadowBase):
    """Delta-strike wheel shadow (original: wheel_delta_paper_trade.py)."""
    id = "btc_wheel_delta_v2"

    def _select(self, idx: float) -> dict | None:
        from scripts.wheel_delta_paper_trade import pick_strangle_delta
        return pick_strangle_delta(idx)
