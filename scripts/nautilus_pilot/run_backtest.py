#!/usr/bin/env python3
"""NautilusTrader 1.230.0 backtest of the ETH momentum sniper.

Low-level BacktestEngine API, SIM margin venue (USDT base, 50x leverage),
taker fee 0.0005 on the instrument. Runs two variants:
  1. liq_cap=False — honest stop accounting (full 3.5*ATR stop-loss hit)
  2. liq_cap=True  — emulates the reference engine's 95%-of-margin loss cap
                     by tightening the initial stop to the equivalent price

Usage:
    .venv-nautilus/Scripts/python.exe scripts/nautilus_pilot/run_backtest.py
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USDT
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.objects import Money

from load_data import VENUE, load_bars, make_bar_type, make_instrument
from eth_sniper_nautilus import EthSniperConfig, EthSniperStrategy

INITIAL_CAPITAL = 100_000.0
LEVERAGE = Decimal(50)
OUT_FILE = Path(__file__).parent / "nautilus_results.json"


def compute_stats(equity_curve: list[tuple[int, float]], trades: list[dict]) -> dict:
    eq = pd.Series(
        [e for _, e in equity_curve],
        index=pd.to_datetime([ts for ts, _ in equity_curve], utc=True),
    )
    daily = eq.resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    total_ret = daily.iloc[-1] / daily.iloc[0] - 1
    n_days = (daily.index[-1] - daily.index[0]).days
    cagr = (1 + total_ret) ** (365 / max(n_days, 1)) - 1
    sharpe = float(rets.mean() / rets.std() * np.sqrt(365)) if rets.std() > 0 else 0.0
    cum = (1 + rets).cumprod()
    max_dd = float(((cum - cum.cummax()) / cum.cummax()).min())

    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = abs(sum(t["pnl"] for t in losses))
    return {
        "final_equity": round(float(eq.iloc[-1]), 2),
        "total_return_pct": round(total_ret * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "sharpe_daily_365": round(sharpe, 3),
        "max_dd_pct": round(max_dd * 100, 2),
        "n_trades": n,
        "win_rate": round(len(wins) / max(n, 1), 4),
        "profit_factor": round(gross_w / max(gross_l, 1e-9), 3),
        "n_short": sum(1 for t in trades if t["dir"] < 0),
        "avg_bars": round(float(np.mean([t["bars"] for t in trades])) if n else 0, 1),
    }


def run_variant(liq_cap: bool) -> dict:
    instrument = make_instrument()
    bar_type = make_bar_type()

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id="ETHSNIPER-001",
            logging=LoggingConfig(log_level="ERROR"),
        )
    )
    engine.add_venue(
        venue=VENUE,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USDT,
        starting_balances=[Money(INITIAL_CAPITAL, USDT)],
        default_leverage=LEVERAGE,
    )
    engine.add_instrument(instrument)
    engine.add_data(load_bars(instrument, bar_type))

    strategy = EthSniperStrategy(
        EthSniperConfig(
            instrument_id=instrument.id,
            bar_type=bar_type,
            initial_capital=INITIAL_CAPITAL,
            leverage=float(LEVERAGE),
            atr_trail=3.0,
            max_hold=180,
            liq_cap=liq_cap,
        )
    )
    engine.add_strategy(strategy)
    engine.run()

    stats = compute_stats(strategy.equity_curve, strategy.closed_trades)
    stats["entry_types"] = strategy.entry_types
    stats["exit_reasons"] = strategy.exit_reasons

    # Cross-check final account balance from the venue
    account_report = engine.trader.generate_account_report(VENUE)
    if not account_report.empty:
        stats["venue_final_balance"] = float(account_report["total"].iloc[-1])

    engine.dispose()
    return stats


def main() -> None:
    # NOTE: Nautilus' Rust logging layer can only be initialized once per
    # process, so each engine variant must run in its own process.
    import subprocess
    import sys

    if len(sys.argv) > 1 and sys.argv[1] in ("pure_stop", "liq_cap"):
        label = sys.argv[1]
        print(f"\n{'='*64}\n  NAUTILUS RUN: {label}\n{'='*64}")
        stats = run_variant(liq_cap=(label == "liq_cap"))
        for k, v in stats.items():
            print(f"  {k}: {v}")
        part = OUT_FILE.with_name(f"nautilus_results_{label}.json")
        part.write_text(json.dumps(stats, indent=2, default=str))
        return

    results = {}
    for label in ("pure_stop", "liq_cap"):
        subprocess.run([sys.executable, __file__, label], check=True)
        part = OUT_FILE.with_name(f"nautilus_results_{label}.json")
        results[label] = json.loads(part.read_text())

    OUT_FILE.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nSaved -> {OUT_FILE}")


if __name__ == "__main__":
    main()
