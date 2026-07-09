"""Backtest runner for SPY Gamma Scalp Strategy v2.0

Loads real SPY+VIX+IRX data, injects IV and risk-free rate into bar metadata,
runs the event-driven backtest engine, and produces a comprehensive report
with Greek-decomposed P&L, regime analysis, and risk metrics.

Usage:
    python scripts/backtest_gamma_scalp.py
    python scripts/backtest_gamma_scalp.py --data data/sample/SPY_gamma_scalp_daily.csv
    python scripts/backtest_gamma_scalp.py --capital 200000 --dte 7
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.enums import AssetClass, TimeFrame
from core.models import Bar
from strategies.base import StrategyConfig
from strategies.registry import discover_strategies, create_strategy


def load_data(csv_path: str) -> pd.DataFrame:
    """Load SPY gamma scalp CSV with features (quality-guarded)."""
    from data.quality.guards import ensure_bars_valid
    df = pd.read_csv(csv_path, parse_dates=["timestamp"])
    _checked, rep = ensure_bars_valid(
        df.set_index("timestamp"), name="gamma_scalp_csv", stale_after_days=None)
    print(f"  DQ: {rep}")
    df = _checked.reset_index().rename(columns={"index": "timestamp"})
    print(f"  Loaded {len(df)} rows from {csv_path}")
    print(f"  Date range: {df['timestamp'].iloc[0].date()} to {df['timestamp'].iloc[-1].date()}")
    print(f"  SPY: ${df['close'].iloc[0]:.2f} to ${df['close'].iloc[-1]:.2f}")
    if "vix_close" in df.columns:
        print(f"  VIX range: {df['vix_close'].min():.2f} - {df['vix_close'].max():.2f}")
    return df


def run_backtest_sync(
    df: pd.DataFrame,
    initial_cash: float = 100_000,
    params: dict = None,
) -> dict:
    """Run the gamma scalp backtest using analytical P&L tracking.

    Since the strategy uses Black-Scholes synthetic pricing for options,
    we track P&L analytically via the strategy's internal state rather
    than routing through the paper engine (which can only handle equity fills).

    The equity curve = initial_cash + strategy._running_pnl + unrealized_option_pnl.
    """
    discover_strategies()

    # Inject initial_capital into params so strategy can size positions
    params = dict(params or {})
    params["initial_capital"] = initial_cash

    # Create strategy
    config = StrategyConfig(
        strategy_id="gamma_scalp_bt",
        name="SPY Gamma Scalp v2",
        asset_class=AssetClass.EQUITY,
        symbols=["SPY"],
        parameters=params,
        mode="backtest",
    )
    strategy = create_strategy("spy_gamma_scalp", config)
    strategy.initialize()

    equity_curve = []
    trades = []

    # Filter rows with valid data
    valid = df.dropna(subset=["close"]).copy()
    print(f"\n  Running backtest on {len(valid)} bars...")
    print(f"  Initial capital: ${initial_cash:,.0f}")

    prev_n_results = 0  # Track when new trades close

    for idx, row in valid.iterrows():
        ts = row["timestamp"].to_pydatetime()
        spot = float(row["close"])

        # Create bar with metadata
        metadata = {}
        if "implied_vol" in row and not pd.isna(row["implied_vol"]):
            metadata["implied_vol"] = float(row["implied_vol"])
        if "risk_free_rate" in row and not pd.isna(row["risk_free_rate"]):
            metadata["risk_free_rate"] = float(row["risk_free_rate"])
        if "vix_close" in row and not pd.isna(row["vix_close"]):
            metadata["vix_close"] = float(row["vix_close"])

        bar = Bar(
            symbol="SPY",
            timestamp=ts,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=spot,
            volume=float(row.get("volume", 0)),
            timeframe=TimeFrame.D1,
            asset_class=AssetClass.EQUITY,
            metadata=metadata,
        )

        # Feed bar to strategy (updates internal state, RV, etc.)
        strategy.on_bar("SPY", bar)

        # Generate signals (this triggers open/close/hedge decisions)
        targets = strategy.calculate_target_positions()

        # Record trades from closed positions
        n_results = len(strategy._trade_results)
        if n_results > prev_n_results:
            for t in strategy._trade_results[prev_n_results:]:
                trades.append({
                    "timestamp": t["timestamp"],
                    "symbol": "SPY",
                    "side": "SELL_STRADDLE" if t["direction"] == "sell_vol" else "BUY_STRADDLE",
                    "price": t["entry_spot"],
                    "quantity": t["contracts"],
                    "commission": t["txn_costs"],
                    "pnl": t["total_pnl"],
                    "reason": t["reason"],
                })
            prev_n_results = n_results

        # Compute current equity analytically:
        #   realized P&L from closed trades + unrealized P&L from open position
        realized_pnl = strategy._running_pnl

        # Unrealized P&L from open straddle position
        unrealized_pnl = 0.0
        if strategy._straddle_open and strategy._n_contracts > 0:
            from strategies.options.gamma_scalp_strategy import bs_straddle
            T = strategy._time_to_expiry()
            iv = strategy._implied_vol
            rf = strategy._risk_free
            call_px, put_px = bs_straddle(spot, strategy._strike, T, rf, iv)
            notional = strategy._n_contracts * strategy.multiplier
            is_short = (strategy._trade_direction == "sell_vol")
            sign = -1.0 if is_short else 1.0

            option_mtm = (
                (call_px + put_px) - (strategy._entry_call_price + strategy._entry_put_price)
            ) * notional * sign

            unrealized_pnl = option_mtm + strategy._cum_hedge_pnl - strategy._cum_txn_costs

        total_value = initial_cash + realized_pnl + unrealized_pnl

        equity_curve.append({
            "timestamp": ts.isoformat(),
            "total_value": round(total_value, 2),
            "cash": round(initial_cash + realized_pnl, 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "n_positions": 1 if strategy._straddle_open else 0,
        })

    # Compute metrics
    from backtesting.metrics.performance import compute_metrics
    from backtesting.metrics.risk_metrics import compute_risk_metrics

    eq_df = pd.DataFrame(equity_curve)
    eq_df["total_value"] = eq_df["total_value"].astype(float)

    metrics = compute_metrics(eq_df, risk_free_rate=0.045, periods_per_year=252)

    # Risk metrics
    if "returns" not in eq_df.columns:
        eq_df["returns"] = eq_df["total_value"].pct_change().fillna(0)
    risk = compute_risk_metrics(
        eq_df["returns"].dropna(),
        confidence=0.95,
        portfolio_value=initial_cash,
    )

    # Strategy-specific analytics
    trade_results = strategy._trade_results
    if trade_results:
        sell_trades = [t for t in trade_results if t["direction"] == "sell_vol"]
        buy_trades = [t for t in trade_results if t["direction"] == "buy_vol"]

        strategy_analytics = {
            "total_trades": len(trade_results),
            "sell_vol_trades": len(sell_trades),
            "buy_vol_trades": len(buy_trades),
            "sell_vol_pnl": round(sum(t["total_pnl"] for t in sell_trades), 2),
            "buy_vol_pnl": round(sum(t["total_pnl"] for t in buy_trades), 2),
            "sell_win_rate": round(
                sum(1 for t in sell_trades if t["total_pnl"] > 0) / max(len(sell_trades), 1) * 100, 1
            ),
            "buy_win_rate": round(
                sum(1 for t in buy_trades if t["total_pnl"] > 0) / max(len(buy_trades), 1) * 100, 1
            ),
            "avg_gamma_pnl": round(float(np.mean([t["gamma_pnl"] for t in trade_results])), 2) if trade_results else 0,
            "avg_theta_pnl": round(float(np.mean([t["theta_pnl"] for t in trade_results])), 2) if trade_results else 0,
            "avg_vega_pnl": round(float(np.mean([t["vega_pnl"] for t in trade_results])), 2) if trade_results else 0,
            "total_txn_costs": round(sum(t["txn_costs"] for t in trade_results), 2),
            "close_reasons": {},
        }
        for t in trade_results:
            reason = t["reason"]
            strategy_analytics["close_reasons"][reason] = \
                strategy_analytics["close_reasons"].get(reason, 0) + 1
    else:
        strategy_analytics = {"total_trades": 0}

    results = {
        "strategy_id": "gamma_scalp_bt",
        "strategy_name": "SPY Gamma Scalp v2",
        "initial_cash": initial_cash,
        "final_value": eq_df["total_value"].iloc[-1] if len(eq_df) > 0 else initial_cash,
        "equity_curve": equity_curve,
        "trades": trades,
        "metrics": metrics,
        "risk_metrics": risk,
        "strategy_analytics": strategy_analytics,
        "trade_results": trade_results,
        "n_trades": len(trades),
        "n_bars": len(valid),
    }

    return results


def sanitize(obj):
    """Replace inf/nan with 0.0 for JSON serialization."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    if isinstance(obj, float):
        if math.isinf(obj) or math.isnan(obj):
            return 0.0
    return obj


def print_report(results: dict):
    """Print a comprehensive text report."""
    m = results["metrics"]
    r = results.get("risk_metrics", {})
    a = results.get("strategy_analytics", {})

    print("\n" + "=" * 70)
    print("  SPY GAMMA SCALP BACKTEST REPORT")
    print("=" * 70)

    print(f"\n  {'Capital':.<30} ${results['initial_cash']:>12,.0f}")
    print(f"  {'Final Value':.<30} ${results['final_value']:>12,.2f}")
    print(f"  {'Total Return':.<30} {m.get('total_return_pct', 0):>12.2f}%")
    print(f"  {'Annual Return':.<30} {m.get('annual_return_pct', 0):>12.2f}%")
    print(f"  {'Annual Volatility':.<30} {m.get('annual_volatility', 0)*100:>12.2f}%")

    print(f"\n  {'='*40} Risk-Adjusted {'='*15}")
    print(f"  {'Sharpe Ratio':.<30} {m.get('sharpe_ratio', 0):>12.3f}")
    print(f"  {'Sortino Ratio':.<30} {m.get('sortino_ratio', 0):>12.3f}")
    print(f"  {'Calmar Ratio':.<30} {m.get('calmar_ratio', 0):>12.3f}")

    print(f"\n  {'='*40} Drawdown {'='*20}")
    print(f"  {'Max Drawdown':.<30} {m.get('max_drawdown_pct', 0):>12.2f}%")
    print(f"  {'VaR (95%)':.<30} ${r.get('var_95', 0):>12,.0f}")
    print(f"  {'CVaR (95%)':.<30} ${r.get('cvar_95', 0):>12,.0f}")

    print(f"\n  {'='*40} Trading {'='*21}")
    print(f"  {'Total Straddle Trades':.<30} {a.get('total_trades', 0):>12}")
    print(f"  {'  Sell Vol Trades':.<30} {a.get('sell_vol_trades', 0):>12}")
    print(f"  {'  Buy Vol Trades':.<30} {a.get('buy_vol_trades', 0):>12}")
    print(f"  {'Sell Vol P&L':.<30} ${a.get('sell_vol_pnl', 0):>12,.2f}")
    print(f"  {'Buy Vol P&L':.<30} ${a.get('buy_vol_pnl', 0):>12,.2f}")
    print(f"  {'Sell Win Rate':.<30} {a.get('sell_win_rate', 0):>12.1f}%")
    print(f"  {'Buy Win Rate':.<30} {a.get('buy_win_rate', 0):>12.1f}%")
    print(f"  {'Win Rate (overall)':.<30} {m.get('win_rate', 0)*100:>12.1f}%")

    print(f"\n  {'='*40} Greek P&L {'='*19}")
    print(f"  {'Avg Gamma P&L/trade':.<30} ${a.get('avg_gamma_pnl', 0):>12,.2f}")
    print(f"  {'Avg Theta P&L/trade':.<30} ${a.get('avg_theta_pnl', 0):>12,.2f}")
    print(f"  {'Avg Vega P&L/trade':.<30} ${a.get('avg_vega_pnl', 0):>12,.2f}")
    print(f"  {'Total Txn Costs':.<30} ${a.get('total_txn_costs', 0):>12,.2f}")

    close_reasons = a.get("close_reasons", {})
    if close_reasons:
        print(f"\n  {'='*40} Close Reasons {'='*15}")
        for reason, count in sorted(close_reasons.items(), key=lambda x: -x[1]):
            print(f"  {reason:.<30} {count:>12}")

    # Per-trade detail
    trade_results = results.get("trade_results", [])
    if trade_results:
        print(f"\n  {'='*40} Trade Detail {'='*16}")
        print(f"  {'#':>3} {'Dir':>8} {'Entry':>8} {'Exit':>8} {'PnL':>10} {'Reason':>12} {'Gamma':>8} {'Theta':>8} {'Costs':>8}")
        print(f"  {'-'*3} {'-'*8} {'-'*8} {'-'*8} {'-'*10} {'-'*12} {'-'*8} {'-'*8} {'-'*8}")
        for i, t in enumerate(trade_results[:50], 1):
            pnl_str = f"${t['total_pnl']:>+9,.0f}"
            print(f"  {i:>3} {t['direction']:>8} {t['entry_spot']:>8.1f} {t['exit_spot']:>8.1f} "
                  f"{pnl_str} {t['reason']:>12} {t['gamma_pnl']:>+8.0f} {t['theta_pnl']:>+8.0f} {t['txn_costs']:>8.0f}")

    print("\n" + "=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Backtest SPY Gamma Scalp v2")
    parser.add_argument("--data", type=str,
                        default=str(ROOT / "data" / "sample" / "SPY_gamma_scalp_daily.csv"))
    parser.add_argument("--capital", type=float, default=100_000)
    parser.add_argument("--dte", type=int, default=5)
    parser.add_argument("--base-contracts", type=int, default=30)
    parser.add_argument("--sell-threshold", type=float, default=0.25)
    parser.add_argument("--buy-threshold", type=float, default=0.80)
    parser.add_argument("--max-loss-pct", type=float, default=0.20)
    parser.add_argument("--max-notional-pct", type=float, default=0.45)
    parser.add_argument("--take-profit-pct", type=float, default=0.70)
    parser.add_argument("--vix-floor", type=float, default=12.0)
    parser.add_argument("--vix-ceiling", type=float, default=40.0)
    parser.add_argument("--hedge-threshold", type=float, default=0.06)
    parser.add_argument("--output", type=str, default=None)
    args = parser.parse_args()

    print("=" * 70)
    print("  SPY GAMMA SCALP BACKTEST v2.0")
    print("=" * 70)

    # Load data
    print("\n[1/3] Loading data...")
    df = load_data(args.data)

    # Strategy parameters
    params = {
        "dte": args.dte,
        "base_contracts": args.base_contracts,
        "sell_signal_threshold": args.sell_threshold,
        "buy_signal_threshold": args.buy_threshold,
        "max_loss_pct": args.max_loss_pct,
        "max_notional_pct": args.max_notional_pct,
        "take_profit_pct": args.take_profit_pct,
        "vix_floor": args.vix_floor,
        "vix_ceiling": args.vix_ceiling,
        "hedge_delta_threshold": args.hedge_threshold,
    }
    print(f"\n[2/3] Strategy parameters: {params}")

    # Run backtest
    print("\n[3/3] Running backtest...")
    results = run_backtest_sync(df, args.capital, params)

    # Print report
    print_report(results)

    # Save results
    output_path = args.output or str(ROOT / "results" / "gamma_scalp_backtest.json")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(sanitize(results), f, indent=2, default=str)
    print(f"\n  Results saved to: {output_path}")
    print(f"  ({Path(output_path).stat().st_size / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
