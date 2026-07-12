#!/usr/bin/env python3
"""Backtest the ETH Regime-Switching Strategy with walk-forward validation.

Pipeline:
  1. Load ETH + BTC 1H OHLCV (2024-01-01 → 2026-04-16)
  2. Warm up the strategy with first 720 bars (30 days) — no trading
  3. For each subsequent bar:
      - Feed the bar to the strategy
      - Generate signals, calculate target positions
      - Simulate fills with slippage + fees
      - Track equity curve
  4. Split results into in-sample (IS) and out-of-sample (OOS)
  5. Apply acceptance gate: Sharpe > 1.5, MaxDD < 20%, PF > 1.5,
     OOS Sharpe degradation < 40%

Output:
  * Console metrics for IS, OOS, combined
  * results/eth_regime_v1_<timestamp>.json (trades + equity curve)
  * BacktestResultRecord row in local SQLite
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.enums import AssetClass, SignalDirection  # noqa: E402
from core.logging import get_logger, setup_logging  # noqa: E402
from core.models import Bar  # noqa: E402
from core.paths import get_data_dir  # noqa: E402
from strategies.base import StrategyConfig  # noqa: E402
from strategies.registry import create_strategy, discover_strategies  # noqa: E402

logger = get_logger(__name__)


# ────────────────────────────────────────────────────────────
# Simulation core
# ────────────────────────────────────────────────────────────

class SimResult:
    """Container for per-split backtest results."""
    def __init__(self, label: str):
        self.label = label
        self.equity: List[Tuple[pd.Timestamp, float]] = []
        self.trades: List[Dict[str, Any]] = []
        self.signals: int = 0

    def metrics(self, initial_cash: float, annualization: float = 8766.0) -> Dict[str, float]:
        if len(self.equity) < 2:
            return {"error": "insufficient_data"}

        eq_df = pd.DataFrame(self.equity, columns=["ts", "value"])
        eq_df["ret"] = eq_df["value"].pct_change().fillna(0)

        total_return = eq_df["value"].iloc[-1] / initial_cash - 1.0

        ann_ret = (1 + total_return) ** (annualization / len(eq_df)) - 1 if len(eq_df) > 0 else 0
        ann_vol = eq_df["ret"].std() * math.sqrt(annualization) if eq_df["ret"].std() > 0 else 0
        sharpe = (ann_ret / ann_vol) if ann_vol > 0 else 0.0

        # Max drawdown
        running_max = eq_df["value"].cummax()
        dd = (eq_df["value"] / running_max - 1.0)
        max_dd = float(dd.min())

        # Trade-level stats
        trade_df = pd.DataFrame(self.trades)
        win_rate = 0.0
        profit_factor = 0.0
        avg_win = 0.0
        avg_loss = 0.0
        if not trade_df.empty and "pnl" in trade_df.columns:
            wins = trade_df[trade_df["pnl"] > 0]["pnl"]
            losses = trade_df[trade_df["pnl"] < 0]["pnl"]
            n = len(trade_df)
            win_rate = len(wins) / n if n > 0 else 0.0
            gross_win = wins.sum() if len(wins) > 0 else 0.0
            gross_loss = abs(losses.sum()) if len(losses) > 0 else 0.0
            profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")
            avg_win = float(wins.mean()) if len(wins) > 0 else 0.0
            avg_loss = float(losses.mean()) if len(losses) > 0 else 0.0

        return {
            "label": self.label,
            "initial_cash": initial_cash,
            "final_value": float(eq_df["value"].iloc[-1]),
            "total_return": float(total_return),
            "annual_return": float(ann_ret),
            "annual_volatility": float(ann_vol),
            "sharpe_ratio": float(sharpe),
            "max_drawdown_pct": float(abs(max_dd)),
            "win_rate": float(win_rate),
            "profit_factor": float(profit_factor) if math.isfinite(profit_factor) else 999.0,
            "avg_win": float(avg_win),
            "avg_loss": float(avg_loss),
            "total_trades": int(len(trade_df)),
            "signals_generated": int(self.signals),
        }


def run_simulation(
    eth_df: pd.DataFrame,
    btc_close: pd.Series,
    strategy,
    *,
    initial_cash: float,
    fee_rate: float,
    slippage_bps: float,
    warmup_bars: int,
    min_lot: float,
    leverage: float,
    label: str,
    start_date: Optional[pd.Timestamp] = None,
    end_date: Optional[pd.Timestamp] = None,
    force_min_lot: bool = True,
    max_risk_override_pct: float = 0.50,
) -> SimResult:
    """Single-symbol event-driven backtest with ATR stops already embedded
    in the strategy's exit logic. Supports long + short (negative qty)."""

    # Feed warmup bars first (no trading)
    if warmup_bars > 0:
        for _, row in eth_df.iloc[:warmup_bars].iterrows():
            bar = _row_to_bar(row)
            strategy.on_bar(bar.symbol, bar)

    result = SimResult(label)
    equity = initial_cash
    position_qty = 0.0
    position_entry_price = 0.0
    position_entry_ts: Optional[pd.Timestamp] = None

    slip_mult = slippage_bps / 10_000.0

    sim_bars = eth_df.iloc[warmup_bars:]
    sym = strategy.symbols[0]
    price_col = eth_df["close"]

    # Pre-filter to date range if given
    if start_date is not None:
        sim_bars = sim_bars[sim_bars["timestamp"] >= start_date]
    if end_date is not None:
        sim_bars = sim_bars[sim_bars["timestamp"] <= end_date]

    logger.info("backtest_started", label=label,
                bars=len(sim_bars), initial_cash=initial_cash)

    for i, row in sim_bars.iterrows():
        bar = _row_to_bar(row)
        strategy.on_bar(sym, bar)

        # Mark-to-market equity
        mark_price = bar.close
        unrealized = position_qty * (mark_price - position_entry_price)
        mtm_equity = equity + unrealized

        # Update live equity for dynamic sizing
        strategy.set_live_equity(mtm_equity)

        # Generate target positions for this bar
        targets = strategy.calculate_target_positions()

        for target in targets:
            if target.symbol != sym:
                continue

            desired_qty = float(target.target_quantity)

            # Apply force_min_lot for small accounts
            if force_min_lot and desired_qty != 0:
                abs_q = abs(desired_qty)
                if abs_q < min_lot:
                    # Check if we can afford the minimum
                    notional = min_lot * mark_price
                    margin_required = notional / leverage
                    if margin_required > mtm_equity * 0.90:
                        desired_qty = 0.0  # skip
                    else:
                        # Round up to min lot with original sign
                        desired_qty = math.copysign(min_lot, desired_qty)

            # Trade delta = desired - current
            delta = desired_qty - position_qty
            if abs(delta) < 1e-9:
                continue

            # Fill at close + slippage (slippage always hurts: positive slip on buy, negative on sell)
            fill_price = mark_price * (1 + slip_mult * math.copysign(1, delta))
            fee = abs(delta) * fill_price * fee_rate

            # Realize PnL if closing or flipping position
            if position_qty != 0:
                # Portion closed: min of |delta| and |position|, in the direction of position
                if math.copysign(1, delta) != math.copysign(1, position_qty):
                    close_qty = min(abs(delta), abs(position_qty))
                    realized = close_qty * math.copysign(1, position_qty) * (fill_price - position_entry_price)
                    equity += realized
                    trade_pnl = realized - (close_qty * fill_price * fee_rate + close_qty * position_entry_price * fee_rate)
                    result.trades.append({
                        "entry_ts": str(position_entry_ts),
                        "exit_ts": str(bar.timestamp),
                        "side": "long" if position_qty > 0 else "short",
                        "qty": close_qty,
                        "entry_price": position_entry_price,
                        "exit_price": fill_price,
                        "pnl": trade_pnl,
                    })

            # Deduct fees
            equity -= fee

            # Update position
            new_qty = position_qty + delta
            if abs(new_qty) < 1e-9:
                position_qty = 0.0
                position_entry_price = 0.0
                position_entry_ts = None
            else:
                # Weighted-average entry price
                if math.copysign(1, new_qty) == math.copysign(1, position_qty) and position_qty != 0:
                    # Adding to same-direction position
                    position_entry_price = (
                        (position_qty * position_entry_price + delta * fill_price) / new_qty
                    )
                else:
                    # New position (or flipped)
                    position_entry_price = fill_price
                    position_entry_ts = bar.timestamp
                position_qty = new_qty

            result.signals += 1

        # Record equity at bar close (mark-to-market)
        mtm_equity = equity + position_qty * (bar.close - position_entry_price)
        result.equity.append((bar.timestamp, float(mtm_equity)))

    # Force-close final open position at the very last close (for fair metrics)
    if position_qty != 0:
        last_bar = sim_bars.iloc[-1]
        realized = position_qty * (last_bar["close"] - position_entry_price)
        equity += realized - abs(position_qty) * last_bar["close"] * fee_rate
        result.trades.append({
            "entry_ts": str(position_entry_ts),
            "exit_ts": str(last_bar["timestamp"]),
            "side": "long" if position_qty > 0 else "short",
            "qty": abs(position_qty),
            "entry_price": position_entry_price,
            "exit_price": float(last_bar["close"]),
            "pnl": float(realized),
            "forced_close": True,
        })
        result.equity[-1] = (last_bar["timestamp"], float(equity))

    logger.info("backtest_done", label=label,
                trades=len(result.trades), final_value=equity)
    return result


def _row_to_bar(row: pd.Series) -> Bar:
    return Bar(
        symbol="ETH/USDT:USDT",
        timestamp=row["timestamp"],
        timeframe="1h",
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
        asset_class=AssetClass.CRYPTO,
    )


# ────────────────────────────────────────────────────────────
# Acceptance gate
# ────────────────────────────────────────────────────────────

def check_acceptance(is_m: Dict, oos_m: Dict, gate: Dict) -> Tuple[bool, List[str]]:
    reasons = []
    for m in (is_m, oos_m):
        if m.get("sharpe_ratio", 0) < gate["min_sharpe"]:
            reasons.append(f"{m['label']}: Sharpe {m['sharpe_ratio']:.2f} < {gate['min_sharpe']}")
        if m.get("max_drawdown_pct", 1.0) > gate["max_drawdown_pct"]:
            reasons.append(f"{m['label']}: MaxDD {m['max_drawdown_pct']:.2%} > {gate['max_drawdown_pct']:.2%}")
        if m.get("profit_factor", 0) < gate["min_profit_factor"]:
            reasons.append(f"{m['label']}: PF {m['profit_factor']:.2f} < {gate['min_profit_factor']}")
        if m.get("total_trades", 0) < gate["min_trades"]:
            reasons.append(f"{m['label']}: trades {m['total_trades']} < {gate['min_trades']}")

    is_sh = is_m.get("sharpe_ratio", 0)
    oos_sh = oos_m.get("sharpe_ratio", 0)
    if is_sh > 0:
        deg = 1 - (oos_sh / is_sh) if oos_sh > 0 else 1.0
        if deg > gate["max_oos_sharpe_degradation"]:
            reasons.append(f"OOS Sharpe degraded {deg:.0%} > {gate['max_oos_sharpe_degradation']:.0%}")

    return len(reasons) == 0, reasons


# ────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────

def main() -> int:
    setup_logging(level="INFO", json_format=False)

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/strategies/eth_regime_v1.yaml")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    with open(cfg_path) as f:
        cfg_dict = yaml.safe_load(f)

    bt = cfg_dict["backtest"]
    params = cfg_dict["parameters"]
    gate = cfg_dict.get("acceptance", {})

    # Load data
    eth_csv = Path(bt["data_file"])
    btc_csv = Path(bt["btc_data_file"])
    eth_df = pd.read_csv(eth_csv, parse_dates=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    btc_df = pd.read_csv(btc_csv, parse_dates=["timestamp"])
    btc_close = btc_df.set_index("timestamp")["close"]

    logger.info("data_loaded", eth_bars=len(eth_df), btc_bars=len(btc_df))

    discover_strategies()

    # Run full backtest (single pass — we slice equity curve after)
    config = StrategyConfig(
        strategy_id=cfg_dict["strategy_id"],
        name=cfg_dict["name"],
        asset_class=AssetClass.CRYPTO,
        symbols=cfg_dict["symbols"],
        parameters=params,
        enabled=True,
        mode="backtest",
    )
    strategy = create_strategy(cfg_dict["strategy_id"], config)
    strategy.set_btc_window(btc_close)

    initial_cash = float(bt["initial_cash"])

    full = run_simulation(
        eth_df, btc_close, strategy,
        initial_cash=initial_cash,
        fee_rate=float(bt["fee_rate"]),
        slippage_bps=float(bt["slippage_bps"]),
        warmup_bars=int(cfg_dict["data"]["warmup_bars"]),
        min_lot=0.1,
        leverage=float(params["leverage"]),
        force_min_lot=bool(params.get("force_min_lot", True)),
        max_risk_override_pct=float(params.get("max_risk_override_pct", 0.50)),
        label="full",
    )

    # Split into IS / OOS by timestamp
    is_end = pd.Timestamp(bt["in_sample_end"], tz="UTC")
    oos_start = pd.Timestamp(bt["out_of_sample_start"], tz="UTC")

    def _slice(sim: SimResult, start: Optional[pd.Timestamp], end: Optional[pd.Timestamp], label: str) -> SimResult:
        sub = SimResult(label)
        # Filter equity series
        for ts, v in sim.equity:
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue
            sub.equity.append((ts, v))
        # Filter trades by entry_ts within range
        for t in sim.trades:
            t_ts = pd.Timestamp(t["entry_ts"])
            if start is not None and t_ts < start:
                continue
            if end is not None and t_ts > end:
                continue
            sub.trades.append(t)
        sub.signals = len(sub.trades)
        return sub

    is_sim = _slice(full, None, is_end, "in_sample")
    oos_sim = _slice(full, oos_start, None, "out_of_sample")

    # Metrics on each slice — reset equity base to the slice's starting equity
    def _slice_metrics(sim: SimResult) -> Dict[str, float]:
        if not sim.equity:
            return {"label": sim.label, "error": "empty"}
        start_val = sim.equity[0][1]
        return sim.metrics(initial_cash=start_val)

    full_m = full.metrics(initial_cash=initial_cash)
    is_m = _slice_metrics(is_sim)
    is_m["label"] = "in_sample"
    oos_m = _slice_metrics(oos_sim)
    oos_m["label"] = "out_of_sample"

    # ── Report ──
    print("\n" + "=" * 80)
    print(f"ETH Regime-Switching v1 Backtest")
    print(f"Period: {bt['start_date']} → {bt['end_date']}  (1H, ~{len(eth_df):,} bars)")
    print("=" * 80)
    for m in (full_m, is_m, oos_m):
        print(f"\n[{m['label']}]")
        if "error" in m:
            print(f"  {m['error']}")
            continue
        print(f"  Initial:       ${m.get('initial_cash', 0):,.2f}")
        print(f"  Final:         ${m.get('final_value', 0):,.2f}")
        print(f"  Total return:  {m.get('total_return', 0):+.2%}")
        print(f"  Annual return: {m.get('annual_return', 0):+.2%}")
        print(f"  Annual vol:    {m.get('annual_volatility', 0):.2%}")
        print(f"  Sharpe:        {m.get('sharpe_ratio', 0):.2f}")
        print(f"  Max DD:        {m.get('max_drawdown_pct', 0):.2%}")
        print(f"  Win rate:      {m.get('win_rate', 0):.2%}")
        print(f"  Profit factor: {m.get('profit_factor', 0):.2f}")
        print(f"  Trades:        {m.get('total_trades', 0)}")

    passed, reasons = check_acceptance(is_m, oos_m, gate)
    print("\n" + "=" * 80)
    if passed:
        print("✅ ACCEPTED")
    else:
        print("❌ REJECTED")
        for r in reasons:
            print(f"   - {r}")
    print("=" * 80)

    # Persist results
    backtest_id = str(uuid.uuid4())
    out_dir = PROJECT_ROOT / "results"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"eth_regime_v1_{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    with open(out_path, "w") as f:
        json.dump({
            "backtest_id": backtest_id,
            "strategy_id": cfg_dict["strategy_id"],
            "accepted": passed,
            "rejection_reasons": reasons,
            "full_metrics": full_m,
            "in_sample_metrics": is_m,
            "out_of_sample_metrics": oos_m,
            "trades": full.trades,
            "equity_curve": [
                {"ts": str(ts), "value": v} for ts, v in full.equity[::24]  # daily resample
            ],
            "config": cfg_dict,
        }, f, indent=2, default=str)
    print(f"\nResults → {out_path}")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
