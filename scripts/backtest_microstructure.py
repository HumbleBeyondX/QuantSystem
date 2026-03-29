"""Backtest runner for Crypto Microstructure Mid-Frequency Strategy.

Loads historical 5-minute OHLCV data, computes factor proxies from bars,
and simulates trading with realistic cost modeling.

Since historical orderbook data is unavailable for free, this backtest uses
OHLCV-based factor proxies (see data/features/microstructure_proxy.py).
The results provide directional alpha validation — if the strategy can't
profit with proxy factors, it won't profit with real orderbook data either.
Conversely, if it shows alpha, paper trading with real orderbook data will
determine if the alpha survives with actual microstructure signals.

Usage:
    # Step 1: Download data (run once)
    python scripts/fetch_crypto_data.py --symbol BTC/USDT --timeframe 5m --start 2023-01-01 --end 2026-03-27
    python scripts/fetch_crypto_data.py --symbol ETH/USDT --timeframe 5m --start 2023-01-01 --end 2026-03-27

    # Step 2: Run backtest
    python scripts/backtest_microstructure.py
    python scripts/backtest_microstructure.py --symbol BTC/USDT --capital 10000
    python scripts/backtest_microstructure.py --data data/crypto/BTC_USDT_5m_2023-01-01_to_2026-03-27.csv
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.features.microstructure_midfreq import compute_composite_score
from data.features.microstructure_proxy import compute_all_proxies
from execution.models.cost_model import CostModel


# ============================================================
# Configuration
# ============================================================

DEFAULT_PARAMS = {
    # Factor weights
    "weight_obi": 0.30,
    "weight_tfi": 0.25,
    "weight_microprice_dev": 0.20,
    "weight_whale": 0.10,
    "weight_spread_zscore": 0.10,
    "weight_vpin": 0.05,

    # Signal thresholds
    "entry_threshold": 0.35,
    "exit_threshold": 0.15,
    "composite_scale": 50.0,

    # Cost model (Binance VIP0)
    "maker_fee_rate": 0.0002,
    "taker_fee_rate": 0.0004,
    "base_slippage_bps": 1.0,
    "depth_impact_factor": 0.05,
    "min_profit_to_cost_ratio": 2.0,

    # Position sizing
    "max_position_usd": 5000.0,

    # Risk management
    "stop_loss_bps": 30.0,
    "take_profit_bps": 80.0,
    "max_daily_trades": 50,
    "cooldown_bars": 3,
    "max_consecutive_losses": 3,
    "extended_cooldown_bars": 30,

    # Warmup
    "min_warmup_bars": 100,
    "vpin_n_buckets": 50,
}


# ============================================================
# Backtest Engine
# ============================================================


class MicrostructureBacktest:
    """Custom backtest engine for the microstructure strategy.

    Uses OHLCV-based factor proxies for signal generation and
    tracks P&L with realistic cost modeling.
    """

    def __init__(
        self,
        initial_capital: float = 10_000,
        params: Dict[str, Any] = None,
    ):
        self.initial_capital = initial_capital
        self.p = {**DEFAULT_PARAMS, **(params or {})}

        # Cost model
        self.cost_model = CostModel(
            maker_fee=self.p["maker_fee_rate"],
            taker_fee=self.p["taker_fee_rate"],
            base_slippage_bps=self.p["base_slippage_bps"],
            depth_impact_factor=self.p["depth_impact_factor"],
            min_profit_to_cost_ratio=self.p["min_profit_to_cost_ratio"],
        )

        # State
        self.cash = initial_capital
        self.position = 0.0          # Signed quantity (positive=long, negative=short)
        self.entry_price = 0.0
        self.entry_time = None
        self.entry_composite = 0.0

        # Tracking
        self.equity_curve: List[Dict[str, Any]] = []
        self.trades: List[Dict[str, Any]] = []
        self.factor_log: List[Dict[str, Any]] = []

        # Risk state
        self.daily_trades = 0
        self.last_trade_day = ""
        self.cooldown_remaining = 0
        self.consecutive_losses = 0

    def run(self, df: pd.DataFrame, symbol: str = "BTC/USDT") -> Dict[str, Any]:
        """Run the backtest on a DataFrame of OHLCV data.

        Args:
            df: DataFrame with columns: timestamp, open, high, low, close, volume.
            symbol: Trading symbol for logging.

        Returns:
            Dict with equity_curve, trades, metrics, and factor analysis.
        """
        print(f"\n{'='*60}")
        print(f"  MICROSTRUCTURE BACKTEST — {symbol}")
        print(f"{'='*60}")
        print(f"  Capital: ${self.initial_capital:,.0f}")
        print(f"  Bars: {len(df):,}")
        print(f"  Period: {df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]}")
        print(f"  Entry threshold: {self.p['entry_threshold']}")
        print(f"  Stop loss: {self.p['stop_loss_bps']} bps")
        print(f"  Min profit/cost ratio: {self.p['min_profit_to_cost_ratio']}x")
        print()

        # Build bar list for rolling factor computation
        bar_buffer: List[Dict[str, float]] = []

        for idx in range(len(df)):
            row = df.iloc[idx]
            ts = row["timestamp"]

            bar = {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
            bar_buffer.append(bar)

            # Reset daily counter
            day_str = str(ts)[:10]
            if day_str != self.last_trade_day:
                self.daily_trades = 0
                self.last_trade_day = day_str

            # Skip warmup period
            if len(bar_buffer) < self.p["min_warmup_bars"]:
                self._record_equity(ts, bar["close"])
                continue

            # Cooldown
            if self.cooldown_remaining > 0:
                self.cooldown_remaining -= 1
                self._manage_position(bar["close"], ts)
                self._record_equity(ts, bar["close"])
                continue

            # Compute factors
            factors = compute_all_proxies(bar_buffer, vpin_n_buckets=self.p["vpin_n_buckets"])

            # Composite score
            weights = {
                "obi": self.p["weight_obi"],
                "tfi": self.p["weight_tfi"],
                "microprice_dev": self.p["weight_microprice_dev"],
                "whale": self.p["weight_whale"],
                "spread_zscore": self.p["weight_spread_zscore"],
                "vpin": self.p["weight_vpin"],
            }
            composite = compute_composite_score(
                obi=factors["obi"],
                tfi=factors["tfi"],
                spread_zscore=factors["spread_zscore"],
                microprice_dev=factors["microprice_dev"],
                whale_imbalance=factors["whale_imbalance"],
                vpin=factors["vpin"],
                weights=weights,
            )

            # Log factors periodically (every 100 bars to save memory)
            if idx % 100 == 0:
                self.factor_log.append({
                    "timestamp": str(ts),
                    "composite": round(composite, 4),
                    **{k: round(v, 4) for k, v in factors.items()},
                })

            price = bar["close"]

            # Position management (stop loss / take profit / signal exit)
            if abs(self.position) > 1e-10:
                self._manage_position(price, ts, composite)
            else:
                # Entry logic
                self._evaluate_entry(price, ts, composite, factors)

            self._record_equity(ts, price)

            # Progress
            if idx % 50_000 == 0 and idx > 0:
                pct = idx / len(df) * 100
                eq = self._current_equity(price)
                ret = (eq / self.initial_capital - 1) * 100
                print(f"  Progress: {pct:.0f}% | Equity: ${eq:,.0f} ({ret:+.2f}%) | Trades: {len(self.trades)}")

        # Final summary
        final_price = float(df.iloc[-1]["close"])
        if abs(self.position) > 1e-10:
            self._close_position(final_price, df.iloc[-1]["timestamp"], "backtest_end")

        return self._compute_results(symbol)

    def _evaluate_entry(
        self, price: float, ts: Any, composite: float, factors: Dict[str, float]
    ) -> None:
        """Evaluate entry signal."""
        if self.daily_trades >= self.p["max_daily_trades"]:
            return

        if abs(composite) < self.p["entry_threshold"]:
            return

        # Expected move
        expected_move_bps = abs(composite) * self.p["composite_scale"]

        # Position size (simplified for backtest — assume decent depth)
        target_usd = self.p["max_position_usd"] * abs(composite)
        target_usd = min(target_usd, self.cash * 0.95)  # Don't use more than 95% of cash
        quantity = target_usd / price

        if quantity < 1e-8 or target_usd < 50:  # Minimum $50 trade
            return

        # Cost gate
        # Estimate depth as a fraction of daily volume (rough proxy)
        est_depth = quantity * 10  # Assume we're ~10% of available depth
        rt_cost = self.cost_model.estimate_round_trip_cost_bps(price, quantity, est_depth)

        if expected_move_bps < self.p["min_profit_to_cost_ratio"] * rt_cost:
            return

        # Enter position
        direction = 1.0 if composite > 0 else -1.0
        self.position = direction * quantity
        self.entry_price = price
        self.entry_time = ts
        self.entry_composite = composite
        self.daily_trades += 1

        # Deduct estimated entry cost
        entry_cost = price * quantity * self.p["taker_fee_rate"]
        self.cash -= entry_cost

    def _manage_position(self, price: float, ts: Any, composite: float = 0.0) -> None:
        """Manage open position: check stop loss, take profit, signal exit."""
        if abs(self.position) < 1e-10:
            return

        is_long = self.position > 0
        pnl_bps = (price - self.entry_price) / self.entry_price * 10_000
        if not is_long:
            pnl_bps = -pnl_bps

        exit_reason = None

        # Stop loss
        if pnl_bps <= -self.p["stop_loss_bps"]:
            exit_reason = "stop_loss"

        # Take profit
        elif pnl_bps >= self.p["take_profit_bps"]:
            exit_reason = "take_profit"

        # Signal reversal
        elif is_long and composite < -self.p["exit_threshold"]:
            exit_reason = "signal_reversal"
        elif not is_long and composite > self.p["exit_threshold"]:
            exit_reason = "signal_reversal"

        # Signal decay
        elif abs(composite) < self.p["exit_threshold"] * 0.5 and abs(self.position) > 1e-10:
            # Only exit on decay if position has been held for at least a few bars
            if self.entry_time and str(ts) > str(self.entry_time):
                exit_reason = "signal_decay"

        if exit_reason:
            self._close_position(price, ts, exit_reason)

    def _close_position(self, price: float, ts: Any, reason: str) -> None:
        """Close position and record trade."""
        if abs(self.position) < 1e-10:
            return

        is_long = self.position > 0
        quantity = abs(self.position)

        # P&L
        if is_long:
            gross_pnl = (price - self.entry_price) * quantity
        else:
            gross_pnl = (self.entry_price - price) * quantity

        # Exit cost
        exit_cost = price * quantity * self.p["taker_fee_rate"]
        net_pnl = gross_pnl - exit_cost

        pnl_bps = (price - self.entry_price) / self.entry_price * 10_000
        if not is_long:
            pnl_bps = -pnl_bps

        self.trades.append({
            "entry_time": str(self.entry_time),
            "exit_time": str(ts),
            "direction": "LONG" if is_long else "SHORT",
            "entry_price": round(self.entry_price, 2),
            "exit_price": round(price, 2),
            "quantity": round(quantity, 6),
            "gross_pnl": round(gross_pnl, 2),
            "net_pnl": round(net_pnl, 2),
            "pnl_bps": round(pnl_bps, 2),
            "exit_cost": round(exit_cost, 4),
            "reason": reason,
            "composite_at_entry": round(self.entry_composite, 4),
        })

        # Update cash
        self.cash += gross_pnl - exit_cost

        # Cooldown logic
        if net_pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.p["max_consecutive_losses"]:
                self.cooldown_remaining = self.p["extended_cooldown_bars"]
                self.consecutive_losses = 0
            else:
                self.cooldown_remaining = self.p["cooldown_bars"] * (1 + self.consecutive_losses)
        else:
            self.consecutive_losses = 0

        # Reset position
        self.position = 0.0
        self.entry_price = 0.0
        self.entry_time = None

    def _current_equity(self, current_price: float) -> float:
        """Compute current equity including unrealized P&L."""
        unrealized = 0.0
        if abs(self.position) > 1e-10:
            if self.position > 0:
                unrealized = (current_price - self.entry_price) * self.position
            else:
                unrealized = (self.entry_price - current_price) * abs(self.position)
        return self.cash + unrealized

    def _record_equity(self, ts: Any, price: float) -> None:
        """Record equity curve data point."""
        equity = self._current_equity(price)
        self.equity_curve.append({
            "timestamp": str(ts),
            "equity": round(equity, 2),
            "cash": round(self.cash, 2),
            "position": round(self.position, 6),
            "price": round(price, 2),
        })

    def _compute_results(self, symbol: str) -> Dict[str, Any]:
        """Compute final results and metrics."""
        eq_df = pd.DataFrame(self.equity_curve)
        eq_series = eq_df["equity"].astype(float)

        # Basic metrics
        total_return = (eq_series.iloc[-1] / eq_series.iloc[0]) - 1
        returns = eq_series.pct_change().dropna()

        n_bars = len(eq_series)

        # Annualize: 5min bars → 288 bars/day × 365 crypto days
        bars_per_year = 288 * 365
        if len(returns) > 1:
            annual_return = (1 + total_return) ** (bars_per_year / n_bars) - 1
            daily_returns = returns.rolling(288).sum().dropna()
            annual_vol = returns.std() * np.sqrt(bars_per_year) if returns.std() > 0 else 0
            sharpe = (returns.mean() / returns.std() * np.sqrt(bars_per_year)) if returns.std() > 0 else 0
        else:
            annual_return = 0
            annual_vol = 0
            sharpe = 0

        # Sortino
        downside = returns[returns < 0]
        downside_std = downside.std() if len(downside) > 0 else 0
        sortino = (returns.mean() / downside_std * np.sqrt(bars_per_year)) if downside_std > 0 else 0

        # Max drawdown
        peak = eq_series.expanding().max()
        drawdown = (eq_series - peak) / peak
        max_dd = drawdown.min()

        # Calmar
        calmar = annual_return / abs(max_dd) if abs(max_dd) > 0 else 0

        # Trade analysis
        trade_df = pd.DataFrame(self.trades) if self.trades else pd.DataFrame()
        n_trades = len(self.trades)

        if n_trades > 0:
            wins = [t for t in self.trades if t["net_pnl"] > 0]
            losses = [t for t in self.trades if t["net_pnl"] <= 0]
            win_rate = len(wins) / n_trades * 100
            avg_win = np.mean([t["net_pnl"] for t in wins]) if wins else 0
            avg_loss = np.mean([t["net_pnl"] for t in losses]) if losses else 0
            profit_factor = abs(sum(t["net_pnl"] for t in wins)) / abs(sum(t["net_pnl"] for t in losses)) if losses and sum(t["net_pnl"] for t in losses) != 0 else float("inf")
            total_fees = sum(t["exit_cost"] for t in self.trades) + sum(
                abs(t["entry_price"]) * abs(t["quantity"]) * self.p["taker_fee_rate"]
                for t in self.trades
            )

            # By direction
            longs = [t for t in self.trades if t["direction"] == "LONG"]
            shorts = [t for t in self.trades if t["direction"] == "SHORT"]

            # By exit reason
            reasons = {}
            for t in self.trades:
                r = t["reason"]
                if r not in reasons:
                    reasons[r] = {"count": 0, "pnl": 0}
                reasons[r]["count"] += 1
                reasons[r]["pnl"] += t["net_pnl"]
        else:
            win_rate = 0
            avg_win = avg_loss = profit_factor = total_fees = 0
            longs = shorts = []
            reasons = {}

        results = {
            "symbol": symbol,
            "period": f"{eq_df['timestamp'].iloc[0]} to {eq_df['timestamp'].iloc[-1]}",
            "initial_capital": self.initial_capital,
            "final_equity": round(eq_series.iloc[-1], 2),
            "total_return_pct": round(total_return * 100, 2),
            "annual_return_pct": round(annual_return * 100, 2),
            "annual_vol_pct": round(annual_vol * 100, 2),
            "sharpe_ratio": round(sharpe, 2),
            "sortino_ratio": round(sortino, 2),
            "max_drawdown_pct": round(max_dd * 100, 2),
            "calmar_ratio": round(calmar, 2),
            "total_trades": n_trades,
            "win_rate_pct": round(win_rate, 1),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "inf",
            "total_fees_paid": round(total_fees, 2),
            "long_trades": len(longs),
            "short_trades": len(shorts),
            "long_pnl": round(sum(t["net_pnl"] for t in longs), 2) if longs else 0,
            "short_pnl": round(sum(t["net_pnl"] for t in shorts), 2) if shorts else 0,
            "exit_reasons": {k: {"count": v["count"], "pnl": round(v["pnl"], 2)} for k, v in reasons.items()},
        }

        return results


# ============================================================
# Output & Reporting
# ============================================================


def print_report(results: Dict[str, Any]) -> None:
    """Pretty-print backtest results."""
    print(f"\n{'='*60}")
    print(f"  BACKTEST RESULTS — {results['symbol']}")
    print(f"{'='*60}")
    print(f"  Period:          {results['period']}")
    print(f"  Initial Capital: ${results['initial_capital']:,.0f}")
    print(f"  Final Equity:    ${results['final_equity']:,.0f}")
    print(f"  Total Return:    {results['total_return_pct']:+.2f}%")
    print(f"  Annual Return:   {results['annual_return_pct']:+.2f}%")
    print(f"  Annual Vol:      {results['annual_vol_pct']:.2f}%")

    print(f"\n  {'─'*40}")
    print(f"  Risk-Adjusted Metrics:")
    print(f"    Sharpe Ratio:  {results['sharpe_ratio']:.2f}")
    print(f"    Sortino Ratio: {results['sortino_ratio']:.2f}")
    print(f"    Calmar Ratio:  {results['calmar_ratio']:.2f}")
    print(f"    Max Drawdown:  {results['max_drawdown_pct']:.2f}%")

    print(f"\n  {'─'*40}")
    print(f"  Trade Statistics:")
    print(f"    Total Trades:  {results['total_trades']}")
    print(f"    Win Rate:      {results['win_rate_pct']:.1f}%")
    print(f"    Avg Win:       ${results['avg_win']:+.2f}")
    print(f"    Avg Loss:      ${results['avg_loss']:+.2f}")
    print(f"    Profit Factor: {results['profit_factor']}")
    print(f"    Total Fees:    ${results['total_fees_paid']:,.2f}")

    print(f"\n  {'─'*40}")
    print(f"  Direction Breakdown:")
    print(f"    Long:  {results['long_trades']} trades, P&L: ${results['long_pnl']:+,.2f}")
    print(f"    Short: {results['short_trades']} trades, P&L: ${results['short_pnl']:+,.2f}")

    if results["exit_reasons"]:
        print(f"\n  {'─'*40}")
        print(f"  Exit Reasons:")
        for reason, data in sorted(results["exit_reasons"].items()):
            print(f"    {reason:20s}: {data['count']:4d} trades, P&L: ${data['pnl']:+,.2f}")

    print(f"\n{'='*60}\n")


# ============================================================
# Main
# ============================================================


def main():
    parser = argparse.ArgumentParser(description="Backtest Crypto Microstructure Strategy")
    parser.add_argument("--data", default=None, help="Path to CSV data file")
    parser.add_argument("--symbol", default="BTC/USDT", help="Trading symbol")
    parser.add_argument("--capital", type=float, default=10_000, help="Initial capital USD")
    parser.add_argument("--entry-threshold", type=float, default=None, help="Override entry threshold")
    parser.add_argument("--stop-loss-bps", type=float, default=None, help="Override stop loss bps")
    parser.add_argument("--max-position", type=float, default=None, help="Override max position USD")
    parser.add_argument("--output", default=None, help="Save results JSON to this path")
    parser.add_argument("--fetch", action="store_true", help="Fetch data first if not found")
    args = parser.parse_args()

    # Find or fetch data
    if args.data:
        data_path = Path(args.data)
    else:
        safe_symbol = args.symbol.replace("/", "_")
        data_dir = ROOT / "data" / "crypto"
        # Look for existing data files
        candidates = sorted(data_dir.glob(f"{safe_symbol}_5m_*.csv")) if data_dir.exists() else []
        if candidates:
            data_path = candidates[-1]  # Use most recent
            print(f"  Found data: {data_path}")
        elif args.fetch:
            print("  Data not found, fetching from Binance...")
            from scripts.fetch_crypto_data import fetch_ohlcv_sync
            fetch_ohlcv_sync(symbol=args.symbol, timeframe="5m")
            candidates = sorted(data_dir.glob(f"{safe_symbol}_5m_*.csv"))
            data_path = candidates[-1]
        else:
            print(f"\n  ERROR: No data found for {args.symbol}.")
            print(f"  Run first: python scripts/fetch_crypto_data.py --symbol {args.symbol}")
            print(f"  Or add --fetch flag to auto-download.")
            sys.exit(1)

    # Load data
    print(f"\n  Loading: {data_path}")
    df = pd.read_csv(data_path, parse_dates=["timestamp"])
    print(f"  Loaded {len(df):,} bars")

    # Override params
    params = {}
    if args.entry_threshold is not None:
        params["entry_threshold"] = args.entry_threshold
    if args.stop_loss_bps is not None:
        params["stop_loss_bps"] = args.stop_loss_bps
    if args.max_position is not None:
        params["max_position_usd"] = args.max_position

    # Run backtest
    bt = MicrostructureBacktest(initial_capital=args.capital, params=params)
    results = bt.run(df, symbol=args.symbol)

    # Print report
    print_report(results)

    # Save results
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = ROOT / "results" / f"microstructure_bt_{args.symbol.replace('/', '_')}.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Save results + equity curve summary (not full curve to save space)
    save_data = {
        "results": results,
        "params": {**DEFAULT_PARAMS, **params},
        "n_equity_points": len(bt.equity_curve),
        "trades": bt.trades[:500],  # First 500 trades for analysis
        "factor_samples": bt.factor_log[:200],  # Factor samples
    }

    with open(output_path, "w") as f:
        json.dump(save_data, f, indent=2, default=str)
    print(f"  Results saved to: {output_path}")

    # Also save full equity curve as CSV for charting
    eq_csv_path = output_path.with_suffix(".equity.csv")
    eq_df = pd.DataFrame(bt.equity_curve)
    # Downsample to hourly for manageable file size
    eq_df["timestamp"] = pd.to_datetime(eq_df["timestamp"])
    eq_hourly = eq_df.set_index("timestamp").resample("1h").last().dropna().reset_index()
    eq_hourly.to_csv(eq_csv_path, index=False)
    print(f"  Equity curve saved to: {eq_csv_path}")


if __name__ == "__main__":
    main()
