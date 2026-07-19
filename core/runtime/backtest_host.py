"""BacktestHost — replay catalog bars through the SAME runtime stack.

The strategy, rails, fill engines and exit mechanics are byte-identical
to the paper runner — backtest/paper parity is architectural. Output is
the platform-standard result dict (equity_curve/trades/metrics) so the
existing tear sheet, records and promotion machinery consume it as-is.

Nautilus cross-check obligation unchanged (scripts/nautilus_pilot).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from core.runtime.context import BacktestContext
from core.runtime.fills import MakerShadow, TakerShadow
from core.runtime.rails import RailsConfig, RiskRails
from core.runtime.strategy import Strategy


def run_backtest(strategy: Strategy,
                 data: Dict[str, pd.DataFrame],
                 initial_equity: float = 10_000.0,
                 rails_cfg: Optional[RailsConfig] = None,
                 half_spread_bps: float = 1.0,
                 funding_series: Optional[Dict[str, pd.Series]] = None,
                 ) -> Dict[str, Any]:
    """data: {symbol: df(timestamp/open/high/low/close/volume)} aligned on
    the strategy's primary timeframe. Bars must be UTC-naive sorted."""
    cfg = rails_cfg or RailsConfig(**getattr(type(strategy), "rails", {}))
    rails = RiskRails(cfg)
    engine = (MakerShadow if getattr(strategy, "maker_only", False)
              else TakerShadow)(cfg)
    ctx = BacktestContext(data, strategy.primary_tf,
                          half_spread_bps=half_spread_bps,
                          funding_series=funding_series)
    n = min(len(df) for df in data.values())
    st = {"equity": initial_equity, "realized_today": 0.0, "day": "",
          "halted": False, "positions": {}, "quotes": {},
          "n_trades": 0, "wins": 0}
    trades: list = []
    curve: list = []

    warmup = strategy.warmup_bars()
    for i in range(warmup, n):
        ctx.cursor = i
        ctx.positions = st["positions"]
        ctx.equity = st["equity"]
        day = str(ctx.now().date())
        if st["day"] != day:
            st["day"], st["realized_today"], st["halted"] = day, 0.0, False

        def _apply(events):
            for ev in events:
                if "pnl" in ev:
                    st["equity"] += ev["pnl"]
                    st["realized_today"] += ev["pnl"]
                    st["n_trades"] += 1
                    st["wins"] += ev["pnl"] > 0
                elif "fee" in ev:
                    st["equity"] -= ev["fee"]
                trades.append(dict(ev))

        _apply(engine.manage(st["positions"], ctx))

        # funding accrual hook (carry strategies) — mirrors the paper runner
        if getattr(strategy, "accrue_funding", False):
            frac = strategy.cadence_sec / 28_800.0
            for sym, pos in st["positions"].items():
                f = ctx.funding(sym)
                if f is None:
                    continue
                mark = float(ctx.current_bar(sym)["open"])
                d = 1 if pos["side"] == "long" else -1
                st["equity"] += -d * f * pos["qty"] * mark * frac
        if st["realized_today"] <= -cfg.daily_loss_cap * initial_equity:
            st["halted"] = True
        intents = strategy.on_tick(ctx) or []
        approved, _ = rails.check(
            intents, st["equity"], st["halted"],
            price_of=lambda s: float(ctx.current_bar(s)["open"]))
        _apply(engine.submit(approved, st["positions"], st["quotes"], ctx))

        unreal = 0.0
        for sym, pos in st["positions"].items():
            px = float(ctx.current_bar(sym)["close"])
            d = 1 if pos["side"] == "long" else -1
            unreal += (px - pos["entry"]) * d * pos["qty"]
        curve.append({"timestamp": str(ctx.now())[:19],
                      "total_value": round(st["equity"] + unreal, 2)})

    # metrics (platform-standard keys)
    eq = pd.Series([c["total_value"] for c in curve],
                   index=pd.to_datetime([c["timestamp"] for c in curve]))
    daily = eq.resample("D").last().dropna()
    r = daily.pct_change().dropna()
    closes = [t for t in trades if str(t.get("action", "")).startswith("CLOSE")]
    wins = [t for t in closes if t.get("pnl", 0) > 0]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = -sum(t["pnl"] for t in closes if t.get("pnl", 0) <= 0)
    metrics = {
        "sharpe_ratio": (round(float(r.mean() / r.std() * np.sqrt(365)), 2)
                         if len(r) > 5 and r.std() > 0 else 0.0),
        "max_drawdown": (round(float((daily / daily.cummax() - 1).min()), 4)
                         if len(daily) else 0.0),
        "total_return_pct": round((st["equity"] / initial_equity - 1) * 100, 2),
        "total_trades": len(closes),
        "win_rate": round(len(wins) / len(closes), 3) if closes else None,
        "profit_factor": (round(gross_w / gross_l, 2)
                          if gross_l > 0 else None),
        "end_value": round(st["equity"], 2),
    }
    return {"strategy_id": strategy.id, "params": dict(strategy.p),
            "initial_cash": initial_equity, "equity_curve": curve,
            "trades": trades, "metrics": metrics,
            "n_trades": len(closes), "final_value": metrics["end_value"]}
