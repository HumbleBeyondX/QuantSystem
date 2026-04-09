#!/usr/bin/env python3
"""Standalone backtest for the BTC/ETH Macro Cycle Strategy.

Runs the macro cycle strategy through historical 4H OHLCV data and produces
a comprehensive performance report with equity curve, drawdown analysis,
and signal diagnostics.

Usage:
    python scripts/backtest_macro_cycle.py
    python scripts/backtest_macro_cycle.py --symbol BTC/USDT --capital 10000
    python scripts/backtest_macro_cycle.py --data data/sample/BTC_USDT_4h.csv
    python scripts/backtest_macro_cycle.py --sweep   # Parameter sweep mode
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

import numpy as np
import pandas as pd

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from data.features.macro_signals import (
    MacroSignalState,
    compute_atr,
    compute_composite_score,
    update_state,
)


class MacroCycleBacktest:
    """Standalone backtest engine for the macro cycle strategy."""

    def __init__(
        self,
        initial_capital: float = 10_000,
        leverage: float = 10.0,
        entry_threshold: float = 0.35,
        exit_threshold: float = -0.10,
        strong_entry_threshold: float = 0.55,
        short_entry_threshold: float = -0.35,
        short_exit_threshold: float = 0.10,
        atr_stop_mult: float = 2.0,
        atr_tp_mult: float = 4.0,
        trailing_stop_atr: float = 1.8,
        base_position_pct: float = 0.10,
        max_position_pct: float = 0.25,
        max_drawdown_pct: float = 0.15,
        cooldown_bars: int = 3,
        max_trades_per_day: int = 3,
        fee_rate: float = 0.0005,  # 5 bps per trade
        signal_weights: Optional[Dict[str, float]] = None,
    ):
        self.initial_capital = initial_capital
        self.leverage = leverage
        self.entry_threshold = entry_threshold
        self.exit_threshold = exit_threshold
        self.strong_entry_threshold = strong_entry_threshold
        self.short_entry_threshold = short_entry_threshold
        self.short_exit_threshold = short_exit_threshold
        self.atr_stop_mult = atr_stop_mult
        self.atr_tp_mult = atr_tp_mult
        self.trailing_stop_atr = trailing_stop_atr
        self.base_position_pct = base_position_pct
        self.max_position_pct = max_position_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.cooldown_bars = cooldown_bars
        self.max_trades_per_day = max_trades_per_day
        self.fee_rate = fee_rate
        self.signal_weights = signal_weights

        # State
        self._state = MacroSignalState()
        self._cash = initial_capital
        self._position = 0.0  # qty in asset
        self._entry_price = 0.0
        self._highest_since_entry = 0.0
        self._lowest_since_entry = 0.0
        self._peak_equity = initial_capital
        self._bars_since_trade = cooldown_bars + 1
        self._daily_trade_count = 0
        self._current_day = ""
        self._circuit_breaker = False

        # Results
        self._equity_curve: List[Dict[str, Any]] = []
        self._trades: List[Dict[str, Any]] = []
        self._signals_log: List[Dict[str, Any]] = []

    def _unrealized_pnl(self, price: float) -> float:
        """Unrealized P&L for leveraged futures position."""
        if abs(self._position) < 1e-12:
            return 0.0
        return (price - self._entry_price) * self._position

    def _equity(self, price: float) -> float:
        """Account equity = cash + unrealized P&L (leveraged futures model).

        In leveraged futures, you don't pay the full notional upfront.
        You only pay margin (notional/leverage) and your P&L fluctuates.
        We track equity as: cash balance + mark-to-market unrealized P&L.
        """
        return self._cash + self._unrealized_pnl(price)

    def _open_position(self, price: float, direction: int, strength: float,
                       atr: float, timestamp: str, composite: float, scores: dict):
        """Open a leveraged position.

        In futures model: only deduct trading fees from cash.
        The position's P&L is tracked via unrealized_pnl, not by
        deducting notional from cash.
        """
        equity = self._equity(price)
        # Cap equity used for sizing to prevent runaway compounding
        # Use min(current_equity, initial_capital * 5) — max 5x growth in sizing
        capped_equity = min(equity, self.initial_capital * 5)
        pos_pct = min(self.base_position_pct * strength, self.max_position_pct)
        notional = capped_equity * pos_pct * self.leverage

        qty = (notional / price) * direction
        cost = abs(qty * price * self.fee_rate)

        self._position = qty
        self._entry_price = price
        self._cash -= cost  # Only deduct fees, not notional (leveraged futures)

        if direction > 0:
            self._highest_since_entry = price
        else:
            self._lowest_since_entry = price

        self._bars_since_trade = 0
        self._daily_trade_count += 1

        self._trades.append({
            "timestamp": timestamp,
            "action": "LONG" if direction > 0 else "SHORT",
            "price": price,
            "quantity": abs(qty),
            "notional": abs(qty * price),
            "leverage_used": abs(qty * price) / max(equity, 1),
            "fee": cost,
            "composite_score": composite,
            "strength": strength,
            "atr": atr,
            "stop_loss": price - direction * self.atr_stop_mult * atr,
            "take_profit": price + direction * self.atr_tp_mult * atr,
            "scores": scores,
        })

    def _close_position(self, price: float, reason: str, timestamp: str,
                        composite: float):
        """Close current position.

        In futures model: realized P&L = (exit - entry) * qty.
        Cash += realized P&L - fees.  (NOT += position * price)
        """
        if abs(self._position) < 1e-12:
            return

        # Realized P&L from the trade
        pnl = (price - self._entry_price) * self._position
        cost = abs(self._position * price * self.fee_rate)

        # Add realized P&L to cash (not the full notional!)
        self._cash += pnl - cost

        direction = "LONG" if self._position > 0 else "SHORT"
        pnl_pct = pnl / max(abs(self._position * self._entry_price), 1) * 100

        self._trades.append({
            "timestamp": timestamp,
            "action": f"CLOSE_{direction}",
            "price": price,
            "quantity": abs(self._position),
            "pnl": pnl - cost,
            "pnl_pct": pnl_pct,
            "fee": cost,
            "reason": reason,
            "composite_score": composite,
            "entry_price": self._entry_price,
            "bars_held": self._bars_since_trade,
        })

        self._position = 0.0
        self._entry_price = 0.0
        self._highest_since_entry = 0.0
        self._lowest_since_entry = 0.0
        self._bars_since_trade = 0

    def run(self, df: pd.DataFrame) -> Dict[str, Any]:
        """Run backtest on DataFrame with OHLCV columns.

        Args:
            df: DataFrame with columns [timestamp, open, high, low, close, volume]

        Returns:
            dict with equity_curve, trades, metrics
        """
        print(f"\n{'='*60}")
        print(f"Running Macro Cycle Backtest")
        print(f"Capital: ${self.initial_capital:,.0f} | Leverage: {self.leverage}x")
        print(f"Bars: {len(df):,} | Timeframe: 4H")
        print(f"{'='*60}\n")

        # Pre-extract columns as numpy arrays for speed
        timestamps = df["timestamp"].astype(str).values if "timestamp" in df.columns else [str(i) for i in range(len(df))]
        opens = df["open"].values.astype(float)
        highs = df["high"].values.astype(float)
        lows = df["low"].values.astype(float)
        closes = df["close"].values.astype(float)
        volumes = df["volume"].values.astype(float) if "volume" in df.columns else np.zeros(len(df))

        for idx in range(len(df)):
            ts = timestamps[idx]
            o, h, l, c, v = opens[idx], highs[idx], lows[idx], closes[idx], volumes[idx]

            # Update macro signal state
            update_state(self._state, c, h, l, v)
            self._bars_since_trade += 1

            # Track trailing stop
            if self._position > 0:
                self._highest_since_entry = max(self._highest_since_entry, h)
            elif self._position < 0:
                self._lowest_since_entry = min(self._lowest_since_entry, l)

            # Update peak equity & circuit breaker
            equity = self._equity(c)
            if equity > self._peak_equity:
                self._peak_equity = equity
            if self._peak_equity > 0:
                dd = (self._peak_equity - equity) / self._peak_equity
                if self._circuit_breaker:
                    # Circuit breaker active — count cooldown bars
                    self._cb_bars = getattr(self, '_cb_bars', 0) + 1
                    if self._cb_bars >= 48 and abs(self._position) < 1e-12:
                        # Cooldown expired (48 bars ≈ 8 days on 4H) + flat → resume
                        # Reset peak to current equity so DD measurement starts fresh
                        self._circuit_breaker = False
                        self._peak_equity = equity
                elif dd > self.max_drawdown_pct:
                    # Circuit breaker triggers — force exit open positions
                    self._circuit_breaker = True
                    self._cb_bars = 0

            # Daily trade count reset
            day = ts[:10]
            if day != self._current_day:
                self._daily_trade_count = 0
                self._current_day = day

            # Skip if not enough data
            if not self._state.initialized:
                self._equity_curve.append({
                    "timestamp": ts,
                    "total_value": equity,
                    "cash": self._cash,
                    "position_value": self._position * c,
                    "n_positions": 1 if abs(self._position) > 1e-10 else 0,
                })
                continue

            # Compute signals
            atr = compute_atr(self._state, 14)
            if atr == 0 or c == 0:
                self._equity_curve.append({
                    "timestamp": ts,
                    "total_value": equity,
                    "cash": self._cash,
                    "position_value": self._position * c,
                    "n_positions": 1 if abs(self._position) > 1e-10 else 0,
                })
                continue

            composite, scores = compute_composite_score(
                self._state, self.signal_weights
            )

            # Log signal every bar
            self._signals_log.append({
                "timestamp": ts,
                "composite": round(composite, 4),
                "price": c,
                **{k: round(v, 4) for k, v in scores.items()},
            })

            # ── TRADING LOGIC ──

            if abs(self._position) < 1e-12:
                # === NO POSITION: ENTRY LOGIC ===
                if (self._bars_since_trade >= self.cooldown_bars
                        and self._daily_trade_count < self.max_trades_per_day
                        and not self._circuit_breaker):

                    if composite >= self.entry_threshold:
                        excess = composite - self.entry_threshold
                        max_excess = 1.0 - self.entry_threshold
                        strength = min(0.3 + 0.7 * (excess / max_excess), 1.0)
                        if composite >= self.strong_entry_threshold:
                            strength = min(strength + 0.2, 1.0)
                        self._open_position(c, 1, strength, atr, ts, composite, scores)

                    elif composite <= self.short_entry_threshold:
                        excess = abs(composite) - abs(self.short_entry_threshold)
                        max_excess = 1.0 - abs(self.short_entry_threshold)
                        strength = min(0.3 + 0.7 * (excess / max_excess), 1.0)
                        self._open_position(c, -1, strength, atr, ts, composite, scores)

            else:
                # === HAS POSITION: EXIT LOGIC ===
                reason = None

                if self._position > 0:
                    # Long exit checks
                    trailing_stop = self._highest_since_entry - self.trailing_stop_atr * atr
                    fixed_stop = self._entry_price - self.atr_stop_mult * atr
                    tp = self._entry_price + self.atr_tp_mult * atr

                    if c < fixed_stop:
                        reason = "stop_loss"
                    elif c < trailing_stop and c > self._entry_price:
                        reason = "trailing_stop"
                    elif c >= tp:
                        reason = "take_profit"
                    elif composite < self.exit_threshold:
                        reason = "signal_reversal"
                    elif self._circuit_breaker:
                        reason = "circuit_breaker"

                elif self._position < 0:
                    # Short exit checks
                    trailing_stop = self._lowest_since_entry + self.trailing_stop_atr * atr
                    fixed_stop = self._entry_price + self.atr_stop_mult * atr
                    tp = self._entry_price - self.atr_tp_mult * atr

                    if c > fixed_stop:
                        reason = "stop_loss"
                    elif c > trailing_stop and c < self._entry_price:
                        reason = "trailing_stop"
                    elif c <= tp:
                        reason = "take_profit"
                    elif composite > self.short_exit_threshold:
                        reason = "signal_reversal"
                    elif self._circuit_breaker:
                        reason = "circuit_breaker"

                if reason:
                    self._close_position(c, reason, ts, composite)

            # Record equity
            equity = self._equity(c)
            self._equity_curve.append({
                "timestamp": ts,
                "total_value": equity,
                "cash": self._cash,
                "position_value": self._position * c,
                "n_positions": 1 if abs(self._position) > 1e-10 else 0,
            })

        # Force close any open position at end
        if abs(self._position) > 1e-10:
            final_price = closes[-1]
            self._close_position(float(final_price), "backtest_end",
                                 timestamps[-1], 0)

        # Compute metrics
        return self._compute_results()

    def _compute_results(self) -> Dict[str, Any]:
        """Compute comprehensive backtest metrics."""
        eq_df = pd.DataFrame(self._equity_curve)
        if eq_df.empty:
            return {"error": "No equity data"}

        equity = eq_df["total_value"]
        returns = equity.pct_change().dropna()

        total_return = (equity.iloc[-1] / equity.iloc[0]) - 1

        # Auto-detect frequency from data spacing
        eq_ts = eq_df.get("timestamp")
        if eq_ts is not None and len(eq_ts) > 2:
            try:
                ts_parsed = pd.to_datetime(eq_ts)
                median_gap_hours = ts_parsed.diff().dropna().median().total_seconds() / 3600
                if median_gap_hours > 18:  # Daily
                    periods_per_year = 365
                elif median_gap_hours > 2:  # 4H
                    periods_per_year = 365 * 6
                elif median_gap_hours > 0.5:  # 1H
                    periods_per_year = 365 * 24
                else:  # sub-hourly
                    periods_per_year = 365 * 24 * 12  # 5min
            except Exception:
                periods_per_year = 365 * 6
        else:
            periods_per_year = 365 * 6
        n_periods = len(returns)
        annual_return = (1 + total_return) ** (periods_per_year / max(n_periods, 1)) - 1

        # Volatility
        daily_vol = returns.std()
        annual_vol = daily_vol * np.sqrt(periods_per_year) if daily_vol > 0 else 0

        # Sharpe (risk-free = 5%)
        daily_rf = 0.05 / periods_per_year
        excess = returns - daily_rf
        sharpe = float(excess.mean() / returns.std() * np.sqrt(periods_per_year)) if returns.std() > 0 else 0

        # Max drawdown
        cumulative = (1 + returns).cumprod()
        peak = cumulative.cummax()
        drawdown = (cumulative - peak) / peak
        max_dd = abs(drawdown.min()) if len(drawdown) > 0 else 0

        # Sortino
        downside = returns[returns < 0]
        downside_vol = downside.std() * np.sqrt(periods_per_year) if len(downside) > 0 else 0
        sortino = float((annual_return - 0.05) / downside_vol) if downside_vol > 0 else 0

        # Trade statistics
        closed_trades = [t for t in self._trades if t["action"].startswith("CLOSE")]
        wins = [t for t in closed_trades if t.get("pnl", 0) > 0]
        losses = [t for t in closed_trades if t.get("pnl", 0) <= 0]
        total_fees = sum(t.get("fee", 0) for t in self._trades)

        win_rate = len(wins) / max(len(closed_trades), 1)
        avg_win = np.mean([t["pnl"] for t in wins]) if wins else 0
        avg_loss = np.mean([t["pnl"] for t in losses]) if losses else 0
        profit_factor = abs(sum(t["pnl"] for t in wins)) / max(abs(sum(t["pnl"] for t in losses)), 1)

        # Exit reason breakdown
        reasons = {}
        for t in closed_trades:
            r = t.get("reason", "unknown")
            reasons[r] = reasons.get(r, 0) + 1

        metrics = {
            "total_return": float(total_return),
            "total_return_pct": float(total_return * 100),
            "annual_return": float(annual_return),
            "annual_return_pct": float(annual_return * 100),
            "annual_volatility": float(annual_vol),
            "sharpe_ratio": float(sharpe),
            "sortino_ratio": float(sortino),
            "max_drawdown": float(max_dd),
            "max_drawdown_pct": float(max_dd * 100),
            "win_rate": float(win_rate),
            "profit_factor": float(profit_factor),
            "avg_win": float(avg_win),
            "avg_loss": float(avg_loss),
            "total_trades": len(closed_trades),
            "total_fees": float(total_fees),
            "n_periods": n_periods,
            "start_value": float(equity.iloc[0]),
            "end_value": float(equity.iloc[-1]),
            "exit_reasons": reasons,
        }

        return {
            "metrics": metrics,
            "equity_curve": self._equity_curve,
            "trades": self._trades,
            "signals_log": self._signals_log[:100],  # First 100 for inspection
        }


def print_report(results: Dict[str, Any]):
    """Print a formatted backtest report."""
    m = results.get("metrics", {})
    trades = results.get("trades", [])

    print(f"\n{'='*60}")
    print(f"  MACRO CYCLE STRATEGY — BACKTEST REPORT")
    print(f"{'='*60}")
    print(f"  Total Return:      {m.get('total_return_pct', 0):+.2f}%")
    print(f"  Annual Return:     {m.get('annual_return_pct', 0):+.2f}%")
    print(f"  Sharpe Ratio:      {m.get('sharpe_ratio', 0):.3f}")
    print(f"  Sortino Ratio:     {m.get('sortino_ratio', 0):.3f}")
    print(f"  Max Drawdown:      {m.get('max_drawdown_pct', 0):.2f}%")
    print(f"  Annual Volatility: {m.get('annual_volatility', 0):.2f}")
    print(f"  Start Value:       ${m.get('start_value', 0):,.2f}")
    print(f"  End Value:         ${m.get('end_value', 0):,.2f}")
    print(f"{'='*60}")
    print(f"  Total Trades:      {m.get('total_trades', 0)}")
    print(f"  Win Rate:          {m.get('win_rate', 0)*100:.1f}%")
    print(f"  Profit Factor:     {m.get('profit_factor', 0):.2f}")
    print(f"  Avg Win:           ${m.get('avg_win', 0):+.2f}")
    print(f"  Avg Loss:          ${m.get('avg_loss', 0):+.2f}")
    print(f"  Total Fees:        ${m.get('total_fees', 0):,.2f}")
    print(f"{'='*60}")

    # Exit reasons
    reasons = m.get("exit_reasons", {})
    if reasons:
        print(f"  Exit Reasons:")
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    {reason:20s}: {count}")
        print(f"{'='*60}")

    # Last 10 trades
    closed = [t for t in trades if t.get("action", "").startswith("CLOSE")]
    if closed:
        print(f"\n  Last 10 Trades:")
        for t in closed[-10:]:
            pnl = t.get("pnl", 0)
            pnl_pct = t.get("pnl_pct", 0)
            print(f"    {t['timestamp'][:16]}  {t['action']:12s}  "
                  f"entry={t.get('entry_price', 0):>10.2f}  "
                  f"exit={t.get('price', 0):>10.2f}  "
                  f"PnL={pnl:+8.2f} ({pnl_pct:+5.1f}%)  "
                  f"reason={t.get('reason', 'n/a')}")
    print()


def run_parameter_sweep(df: pd.DataFrame):
    """Run multiple parameter configurations and compare."""
    configs = [
        {"name": "Conservative", "entry_threshold": 0.45, "leverage": 5.0,
         "atr_stop_mult": 2.5, "atr_tp_mult": 5.0, "trailing_stop_atr": 2.0},
        {"name": "Balanced", "entry_threshold": 0.35, "leverage": 10.0,
         "atr_stop_mult": 2.0, "atr_tp_mult": 4.0, "trailing_stop_atr": 1.8},
        {"name": "Aggressive", "entry_threshold": 0.25, "leverage": 10.0,
         "atr_stop_mult": 1.5, "atr_tp_mult": 3.0, "trailing_stop_atr": 1.5},
        {"name": "High Leverage", "entry_threshold": 0.35, "leverage": 15.0,
         "atr_stop_mult": 1.8, "atr_tp_mult": 3.5, "trailing_stop_atr": 1.5},
        {"name": "Trend Focus", "entry_threshold": 0.35, "leverage": 10.0,
         "atr_stop_mult": 2.0, "atr_tp_mult": 6.0, "trailing_stop_atr": 2.0,
         "signal_weights": {"ma200w": 0.10, "pi_cycle": 0.05, "trend": 0.40,
                           "momentum": 0.25, "volume": 0.05, "drawdown": 0.05,
                           "volatility": 0.10}},
        {"name": "Momentum Focus", "entry_threshold": 0.30, "leverage": 10.0,
         "atr_stop_mult": 2.0, "atr_tp_mult": 4.0, "trailing_stop_atr": 1.8,
         "signal_weights": {"ma200w": 0.05, "pi_cycle": 0.05, "trend": 0.20,
                           "momentum": 0.40, "volume": 0.15, "drawdown": 0.05,
                           "volatility": 0.10}},
    ]

    print(f"\n{'='*80}")
    print(f"  PARAMETER SWEEP — {len(configs)} configurations")
    print(f"{'='*80}")
    print(f"  {'Config':<18s} {'Return':>8s} {'Sharpe':>8s} {'MaxDD':>8s} "
          f"{'WinRate':>8s} {'Trades':>7s} {'PF':>6s} {'EndVal':>10s}")
    print(f"  {'-'*75}")

    results_all = []
    for cfg in configs:
        name = cfg.pop("name")
        bt = MacroCycleBacktest(initial_capital=10000, **cfg)
        result = bt.run(df)
        m = result.get("metrics", {})

        print(f"  {name:<18s} {m.get('total_return_pct', 0):>+7.1f}% "
              f"{m.get('sharpe_ratio', 0):>8.3f} "
              f"{m.get('max_drawdown_pct', 0):>7.1f}% "
              f"{m.get('win_rate', 0)*100:>7.1f}% "
              f"{m.get('total_trades', 0):>7d} "
              f"{m.get('profit_factor', 0):>5.2f} "
              f"${m.get('end_value', 0):>9,.0f}")

        results_all.append({"name": name, **m})

    print(f"  {'-'*75}")

    # Best by Sharpe
    best = max(results_all, key=lambda x: x.get("sharpe_ratio", -999))
    print(f"\n  Best Sharpe: {best['name']} (Sharpe={best.get('sharpe_ratio', 0):.3f})")
    print()

    return results_all


def main():
    parser = argparse.ArgumentParser(description="Macro Cycle Strategy Backtest")
    parser.add_argument("--data", type=str, default=None,
                        help="Path to CSV data file")
    parser.add_argument("--symbol", type=str, default="BTC/USDT")
    parser.add_argument("--capital", type=float, default=10000)
    parser.add_argument("--leverage", type=float, default=10.0)
    parser.add_argument("--sweep", action="store_true", help="Run parameter sweep")
    parser.add_argument("--save", type=str, default=None,
                        help="Save results to JSON file")
    args = parser.parse_args()

    # Find data file
    data_file = args.data
    if not data_file:
        safe = args.symbol.replace("/", "_")
        candidates = [
            PROJECT_ROOT / "data" / "sample" / f"{safe}_4h.csv",
            PROJECT_ROOT / "data" / "sample" / f"{safe}_1h.csv",
            PROJECT_ROOT / "data" / "crypto" / f"{safe}_4h.csv",
        ]
        for c in candidates:
            if c.exists():
                data_file = str(c)
                break

    if not data_file or not Path(data_file).exists():
        print(f"ERROR: No data file found for {args.symbol}")
        print(f"Run: python scripts/fetch_crypto_4h_data.py --symbols {args.symbol}")
        sys.exit(1)

    print(f"Loading data from: {data_file}")
    df = pd.read_csv(data_file, parse_dates=["timestamp"] if "timestamp" in
                     pd.read_csv(data_file, nrows=1).columns else None)

    # Ensure timestamp column
    if "timestamp" not in df.columns:
        for col in ("time", "datetime", "date"):
            if col in df.columns:
                df.rename(columns={col: "timestamp"}, inplace=True)
                break

    df.columns = [c.lower() for c in df.columns]
    print(f"Data shape: {df.shape}")
    print(f"Date range: {df['timestamp'].iloc[0]} → {df['timestamp'].iloc[-1]}")

    if args.sweep:
        run_parameter_sweep(df)
    else:
        bt = MacroCycleBacktest(
            initial_capital=args.capital,
            leverage=args.leverage,
        )
        results = bt.run(df)
        print_report(results)

        if args.save:
            save_path = Path(args.save)
            save_path.parent.mkdir(parents=True, exist_ok=True)
            # Only save metrics and trades, not huge equity curve
            save_data = {
                "metrics": results["metrics"],
                "trades": results["trades"],
                "n_equity_points": len(results["equity_curve"]),
            }
            with open(save_path, "w") as f:
                json.dump(save_data, f, indent=2, default=str)
            print(f"Results saved to: {save_path}")


if __name__ == "__main__":
    main()
