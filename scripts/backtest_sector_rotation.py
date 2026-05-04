#!/usr/bin/env python3
"""Backtest the SectorRotationStrategy using the existing EventDrivenBacktest engine.

Loads the parquet panel (from fetch_sector_etf_data.py), groups bars by
ticker into per-ticker DataFrames, instantiates the strategy with config,
and feeds the EventDrivenBacktest engine. Prints the acceptance gate
verdict and dumps results JSON.

Usage:
    python scripts/backtest_sector_rotation.py
    python scripts/backtest_sector_rotation.py --config config/strategies/sector_rotation_v1.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict

import numpy as np
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtesting.engine.event_driven import EventDrivenBacktest  # noqa: E402
from core.enums import AssetClass  # noqa: E402
from core.logging import get_logger, setup_logging  # noqa: E402
from execution.models.fees import PercentageFee  # noqa: E402
from execution.models.slippage import FixedSlippage  # noqa: E402
from strategies.base import StrategyConfig  # noqa: E402
from strategies.registry import create_strategy, discover_strategies  # noqa: E402

logger = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/strategies/sector_rotation_v1.yaml")
    p.add_argument("--start", default=None, help="Override config start date")
    p.add_argument("--end", default=None, help="Override config end date")
    return p.parse_args()


def load_panel_as_dict(parquet_path: Path,
                       start_date: str,
                       end_date: str) -> Dict[str, pd.DataFrame]:
    """Read the long-format panel parquet and convert to dict[ticker → df]."""
    panel = pd.read_parquet(parquet_path)
    panel["timestamp"] = pd.to_datetime(panel["timestamp"])
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    panel = panel[(panel["timestamp"] >= start) & (panel["timestamp"] <= end)]
    out: Dict[str, pd.DataFrame] = {}
    for ticker, g in panel.groupby("ticker"):
        df = g.sort_values("timestamp").reset_index(drop=True)
        # Keep OHLCV + timestamp; ensure timestamps are unique
        out[ticker] = df.drop_duplicates(subset="timestamp")
    return out


def evaluate_acceptance(metrics: dict, gate: dict) -> tuple:
    reasons = []

    sharpe = metrics.get("sharpe_ratio", 0)
    max_dd = abs(metrics.get("max_drawdown", 0))

    if sharpe < gate.get("min_sharpe", 2.0):
        reasons.append(f"Sharpe {sharpe:.2f} < {gate['min_sharpe']}")
    if max_dd > gate.get("max_drawdown_pct", 0.15):
        reasons.append(f"MaxDD {max_dd:.2%} > {gate['max_drawdown_pct']:.0%}")
    return len(reasons) == 0, reasons


def main() -> int:
    setup_logging(level="INFO", json_format=False)
    args = parse_args()

    cfg_path = Path(args.config)
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    bt = cfg["backtest"]
    start = args.start or bt["start_date"]
    end = args.end or bt["end_date"]
    initial_cash = float(bt["initial_capital"])
    fee_bps = int(bt.get("fee_bps", 5))
    slip_bps = int(bt.get("slippage_bps", 5))
    parquet_path = Path(bt["data_file"])
    if not parquet_path.exists():
        print(f"ERROR: data file not found: {parquet_path}")
        print("Run scripts/fetch_sector_etf_data.py first.")
        return 1

    print(f"Sector Rotation v1 backtest")
    print(f"  Period:     {start} → {end}")
    print(f"  Capital:    ${initial_cash:,.0f}")
    print(f"  Fees:       {fee_bps} bps + {slip_bps} bps slippage")
    print(f"  Parquet:    {parquet_path.name}")
    print()

    # ── Load data ──
    print("Loading panel...", flush=True)
    data = load_panel_as_dict(parquet_path, start, end)
    print(f"  Loaded {len(data)} tickers")
    for t, df in data.items():
        print(f"    {t:<8} {len(df):>5} bars")

    # ── Build strategy ──
    discover_strategies()
    params = {
        "universe": cfg["universe"],
        "hedge_ticker": cfg.get("hedge_ticker", "SPY"),
        "macro_tickers": ["SPY", "^VIX", "^TNX", "^IRX", "HYG", "IEF"],
        "rebalance_cadence_bars": cfg["rebalance"]["cadence_bars"],
        "regime_n_components": cfg["regime"]["n_components"],
        "regime_train_window_days": cfg["regime"]["train_window_days"],
        "regime_refit_every_days": cfg["regime"]["refit_every_days"],
        "regime_factor_weights": cfg["regime_factor_weights"],
        "cov_lookback_days": cfg["mv"]["cov_lookback_days"],
        "weight_min": cfg["mv"]["weight_min"],
        "weight_max": cfg["mv"]["weight_max"],
        "target_volatility": cfg["mv"]["target_volatility"],
        "tilt_strength": cfg["mv"]["tilt_strength"],
        "base_return_lookback_days": cfg["mv"]["base_return_lookback_days"],
        "hedge_enabled": cfg["hedge"]["enabled"],
        "max_short_pct": cfg["hedge"]["max_short_pct"],
        "initial_capital": initial_cash,
    }
    config = StrategyConfig(
        strategy_id=cfg["strategy_id"],
        name=cfg["name"],
        asset_class=AssetClass.EQUITY,
        symbols=list(data.keys()),
        parameters=params,
        enabled=True,
        mode="backtest",
    )
    strategy = create_strategy(cfg["strategy_id"], config)
    print(f"\nStrategy: {type(strategy).__name__}")
    print(f"  Universe: {len(cfg['universe'])} ETFs + SPY hedge\n")

    # ── Run backtest ──
    backtest = EventDrivenBacktest(
        strategy=strategy,
        initial_cash=initial_cash,
        slippage=FixedSlippage(bps=slip_bps),
        fees=PercentageFee(rate=fee_bps / 10_000),
        risk_check=False,  # we trust the MV optimizer's bounds
    )

    t0 = time.time()
    try:
        result = asyncio.run(backtest.run(data))
    except Exception as e:
        print(f"\nBacktest error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    elapsed = time.time() - t0
    print(f"\nBacktest finished in {elapsed:.1f}s")

    # ── Metrics ──
    m = result.get("metrics", {})
    print("\n" + "=" * 80)
    print("RESULTS")
    print("=" * 80)
    print(f"  Initial:           ${initial_cash:,.2f}")
    print(f"  Final:             ${result.get('final_value', initial_cash):,.2f}")
    print(f"  Total return:      {m.get('total_return', 0)*100:+.2f}%")
    print(f"  Annual return:     {m.get('annual_return', 0)*100:+.2f}%")
    print(f"  Sharpe:            {m.get('sharpe_ratio', 0):.3f}")
    print(f"  Sortino:           {m.get('sortino_ratio', 0):.3f}")
    print(f"  Max drawdown:      {abs(m.get('max_drawdown', 0))*100:.2f}%")
    print(f"  Win rate:          {m.get('win_rate', 0)*100:.1f}%")
    print(f"  Profit factor:     {m.get('profit_factor', 0):.2f}")
    print(f"  Trades:            {result.get('n_trades', 0)}")
    print()

    accept_cfg = cfg.get("acceptance", {})
    passed, reasons = evaluate_acceptance(m, accept_cfg)
    print("=" * 80)
    if passed:
        print("✅ ACCEPTED — meets all gate criteria")
    else:
        print("❌ REJECTED")
        for r in reasons:
            print(f"   - {r}")
    print("=" * 80)

    # ── Save ──
    out = PROJECT_ROOT / "results" / f"sector_rotation_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    save_payload = {
        "config": cfg,
        "passed": passed,
        "rejection_reasons": reasons,
        "metrics": m,
        "n_trades": result.get("n_trades", 0),
        "final_value": result.get("final_value", initial_cash),
        "last_diagnostic": getattr(strategy, "last_diagnostic", {}),
        "equity_curve_sample": result.get("equity_curve", [])[::5],  # every 5th point
    }
    with open(out, "w") as f:
        json.dump(save_payload, f, indent=2, default=str)
    print(f"\n→ {out}")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
