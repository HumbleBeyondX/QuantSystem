#!/usr/bin/env python3
"""CLI script to run backtests against historical data.

Usage::

    python scripts/run_backtest.py \\
        --strategy crypto_swing \\
        --data-path data/sample/ \\
        --start-date 2024-01-01 \\
        --end-date 2024-12-31 \\
        --initial-capital 100000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict

import pandas as pd

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backtesting.engine.event_driven import EventDrivenBacktest
from backtesting.metrics.report import generate_text_report
from strategies.base import StrategyConfig
from strategies.registry import create_strategy, discover_strategies, list_strategies

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_backtest")

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_data(
    data_path: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> Dict[str, pd.DataFrame]:
    """Load CSV files from *data_path* into a dict keyed by symbol."""
    path = Path(data_path)
    if path.is_file():
        files = [path]
    elif path.is_dir():
        files = sorted(path.glob("*.csv"))
    else:
        logger.error("Data path does not exist: %s", path)
        sys.exit(1)

    if not files:
        logger.error("No CSV files found in %s", path)
        sys.exit(1)

    data: Dict[str, pd.DataFrame] = {}
    for csv_file in files:
        # Extract symbol: BTC_USDT_1m.csv -> BTC/USDT
        parts = csv_file.stem.split("_")
        symbol = f"{parts[0]}/{parts[1]}" if len(parts) >= 2 else csv_file.stem.replace("_", "/")
        df = pd.read_csv(csv_file, parse_dates=True, index_col=0)
        df.columns = [c.lower() for c in df.columns]

        # Ensure a 'timestamp' column exists for the backtest engine
        if "timestamp" not in df.columns:
            df["timestamp"] = df.index

        # Apply date filters
        if start_date:
            df = df[df["timestamp"] >= pd.Timestamp(start_date)]
        if end_date:
            df = df[df["timestamp"] <= pd.Timestamp(end_date)]

        if df.empty:
            logger.warning("No rows after filtering for %s", symbol)
            continue

        data[symbol] = df.reset_index(drop=True)
        logger.info("Loaded %d bars for %s from %s", len(df), symbol, csv_file.name)

    return data

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a backtest")
    parser.add_argument(
        "--strategy",
        required=True,
        help="Registered strategy name (e.g. crypto_swing)",
    )
    parser.add_argument(
        "--data-path",
        required=True,
        help="Path to CSV file or directory of CSV files",
    )
    parser.add_argument("--start-date", default=None, help="Start date filter (YYYY-MM-DD)")
    parser.add_argument("--end-date", default=None, help="End date filter (YYYY-MM-DD)")
    parser.add_argument(
        "--initial-capital",
        type=float,
        default=100_000.0,
        help="Initial cash (default: 100000)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path to save results JSON (default: results/<strategy>_<timestamp>.json)",
    )
    return parser.parse_args()


async def _run_backtest(args: argparse.Namespace) -> None:
    # Discover and validate strategy
    discover_strategies()
    available = list_strategies()
    if args.strategy not in available:
        logger.error(
            "Strategy '%s' not found. Available: %s", args.strategy, available
        )
        sys.exit(1)

    # Load data
    data = load_data(args.data_path, args.start_date, args.end_date)
    if not data:
        logger.error("No data loaded. Exiting.")
        sys.exit(1)

    symbols = list(data.keys())

    # Create strategy
    config = StrategyConfig(
        strategy_id=f"bt_{args.strategy}",
        name=args.strategy,
        symbols=symbols,
        mode="backtest",
    )
    strategy = create_strategy(args.strategy, config)
    logger.info("Created strategy: %s", strategy)

    # Run backtest
    engine = EventDrivenBacktest(
        strategy=strategy,
        initial_cash=args.initial_capital,
    )
    results = await engine.run(data)

    # Print report
    report = generate_text_report(results)
    print(report)

    # Save results to JSON
    output_path = args.output
    if output_path is None:
        results_dir = PROJECT_ROOT / "results"
        results_dir.mkdir(exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        output_path = str(results_dir / f"{args.strategy}_{ts}.json")

    # Remove non-serialisable items before dumping
    serialisable = {
        k: v for k, v in results.items()
        if k not in ("equity_df",)
    }
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(serialisable, fh, indent=2, default=str)

    logger.info("Results saved to %s", output_path)


def main() -> None:
    args = parse_args()
    asyncio.run(_run_backtest(args))


if __name__ == "__main__":
    main()
