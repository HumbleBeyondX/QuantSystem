"""Tokenized-stock trend scalp — v2 runtime migration.

Same logic as scripts/scalp_paper_runner.py (which keeps running with
its own state until parity cutover): Donchian(10) breakout / EMA(21)
pullback / volume filter on 5m bars, long+short, ATR stops clamped by
rails, ATR trail, time exit.

This is the runtime proving ground: one class, three hosts
(backtest / paper / future live).
"""
from __future__ import annotations

from typing import List

from core.runtime.intents import Intent
from core.runtime.strategy import ParamSpec, Strategy
from strategies_v2 import register


@register
class ScalpTrendV2(Strategy):
    id = "scalp_trend_v2"
    book_type = "linear"
    asset_domain = "tokenized"
    cadence_sec = 300
    universe = ["SPCX", "NVDA", "TSLA", "HOOD"]
    primary_tf = "5m"
    rails = {"leverage": 20.0, "risk_per_trade": 0.01,
             "daily_loss_cap": 0.03, "min_stop_pct": 0.0}

    params_schema = [
        ParamSpec("atr_stop", "float", 1.5, 0.5, 4.0, 0.25,
                  help="initial stop, x ATR (rails clamp inside liquidation)"),
        ParamSpec("atr_trail", "float", 1.0, 0.5, 3.0, 0.25,
                  help="trailing distance, x ATR"),
        ParamSpec("atr_act", "float", 0.5, 0.0, 2.0, 0.25,
                  help="profit (x ATR) before the trail arms"),
        ParamSpec("max_hold_bars", "int", 72, 12, 288, 12,
                  help="time exit, in 5m bars"),
        ParamSpec("min_score", "int", 2, 1, 3, 1,
                  help="entry score threshold (breakout+pullback+volume)"),
        ParamSpec("vol_min", "float", 1.2, 1.0, 2.0, 0.1,
                  help="volume ratio filter"),
    ]

    def warmup_bars(self) -> int:
        return 60

    def on_tick(self, ctx) -> List[Intent]:
        from scripts.backtest_btc_sniper import compute_signals
        intents: List[Intent] = []
        for sym in self.universe:
            if ctx.position(sym):
                continue                     # exits are engine-managed
            df = ctx.bars(sym, self.primary_tf, 80)
            if len(df) < self.warmup_bars():
                continue
            sig = compute_signals(df.copy())
            row = sig.iloc[-1]               # last CLOSED bar
            atr = float(row.get("atr") or 0)
            if atr <= 0:
                continue
            vol_ok = float(row.get("vol_ratio", 0) or 0) >= self.p["vol_min"]
            score_l = (int(bool(row.get("breakout_up", 0)))
                       + int(bool(row.get("near_ema21_up", 0)))
                       + int(vol_ok))
            score_s = (int(bool(row.get("breakout_dn", 0)))
                       + int(bool(row.get("near_ema21_dn", 0)))
                       + int(vol_ok))
            side = ("long" if score_l >= self.p["min_score"]
                    else "short" if score_s >= self.p["min_score"] else None)
            if side:
                intents.append(Intent(
                    action="open", symbol=sym, side=side,
                    stop_dist=self.p["atr_stop"] * atr,
                    trail_dist=self.p["atr_trail"] * atr,
                    trail_activate=self.p["atr_act"] * atr,
                    max_hold_ticks=self.p["max_hold_bars"],
                    tag=f"score{max(score_l, score_s)}"))
        return intents
