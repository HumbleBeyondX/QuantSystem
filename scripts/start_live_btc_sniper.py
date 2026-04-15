#!/usr/bin/env python3
"""Start live trading for BTC Momentum Sniper v5.

This script bootstraps the complete live trading pipeline:
  1. Loads conservative live config
  2. Connects to exchange (testnet or mainnet)
  3. Warms up strategy with 200 historical 4H bars
  4. Runs the live trading loop

SAFETY:
  - Default config uses TESTNET (sandbox: true)
  - $500 initial capital with 10x leverage (not 50x)
  - 2% risk per trade (not 5%)
  - Circuit breaker at 3% daily loss / 10% drawdown
  - Session loss hard cap at $50
  - All withdrawal operations permanently blocked

Usage:
    # Testnet (default — safe for validation):
    python scripts/start_live_btc_sniper.py

    # Custom config:
    python scripts/start_live_btc_sniper.py --config config/live/btc_sniper_live.yaml

    # Override exchange:
    python scripts/start_live_btc_sniper.py --exchange binance

Environment variables required:
    OKX_API_KEY       — Exchange API key
    OKX_API_SECRET    — Exchange API secret
    OKX_PASSPHRASE    — OKX passphrase (OKX only)

    (Or BINANCE_API_KEY/BINANCE_API_SECRET for Binance)

Press Ctrl+C for graceful shutdown (positions stay open).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path

# Add project root to path
project_root = str(Path(__file__).resolve().parent.parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Load .env file BEFORE any imports that read env vars
from dotenv import load_dotenv
env_path = Path(project_root) / ".env"
if env_path.exists():
    load_dotenv(env_path, override=False)

from core.live_trading_runner import LiveTradingRunner
from core.logging import get_logger

logger = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="BTC Momentum Sniper v5 — Live Trading",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default="config/live/btc_sniper_live.yaml",
        help="Path to live trading YAML config",
    )
    parser.add_argument(
        "--exchange",
        choices=["okx", "binance", "bybit"],
        default=None,
        help="Override exchange (overrides config file)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Disable sandbox mode (REAL MONEY). Requires explicit flag for safety.",
    )
    parser.add_argument(
        "--close-on-exit",
        action="store_true",
        help="Close all positions on shutdown (default: keep positions open)",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    # ── Validate config exists ──
    config_path = Path(args.config)
    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)

    # ── Create runner ──
    runner = LiveTradingRunner(str(config_path))
    config = runner.load_config()

    # ── Apply CLI overrides ──
    if args.exchange:
        config["exchange"]["id"] = args.exchange
        logger.info(f"Exchange overridden to: {args.exchange}")

    if args.live:
        config["exchange"]["sandbox"] = False

    sandbox = config["exchange"].get("sandbox", True)
    if not sandbox:
        logger.warning("=" * 60)
        logger.warning("  LIVE MODE — REAL MONEY TRADING!")
        logger.warning("  Exchange: " + config["exchange"]["id"])
        logger.warning("  Press Ctrl+C to stop.")
        logger.warning("=" * 60)
    else:
        logger.info("Running in TESTNET/SANDBOX mode (safe)")

    # ── Verify API keys ──
    exchange_id = config["exchange"]["id"].upper()
    api_key = os.environ.get(f"{exchange_id}_API_KEY", "")
    if not api_key:
        logger.error(
            f"Missing API key. Set {exchange_id}_API_KEY environment variable.\n"
            f"  For testnet: use demo trading API keys.\n"
            f"  OKX demo: https://www.okx.com/account/my-api (switch to demo)\n"
            f"  Binance testnet: https://testnet.binancefuture.com"
        )
        sys.exit(1)

    # ── Safety banner ──
    params = config.get("parameters", {})
    risk = config.get("risk", {})
    print("\n" + "=" * 60)
    print("  BTC Momentum Sniper v5 — Live Trading")
    print("=" * 60)
    print(f"  Exchange:     {config['exchange']['id']}")
    print(f"  Sandbox:      {config['exchange'].get('sandbox', True)}")
    print(f"  Symbol:       {config.get('symbols', ['?'])[0]}")
    print(f"  Leverage:     {params.get('leverage', '?')}x")
    print(f"  Risk/Trade:   {params.get('risk_per_trade', '?'):.0%}")
    print(f"  Capital:      ${params.get('initial_capital', '?')}")
    print(f"  Max DD:       {risk.get('max_drawdown_pct', '?'):.0%}")
    print(f"  Session Cap:  ${risk.get('max_session_loss_usd', '?')}")
    print("=" * 60 + "\n")

    # ── Setup and warmup ──
    try:
        await runner.setup()
        await runner.warmup()
        await runner.sync_exchange_positions()
    except Exception as e:
        logger.error(f"Setup failed: {e}")
        await runner.shutdown()
        sys.exit(1)

    # ── Register signal handlers for graceful shutdown ──
    shutdown_event = asyncio.Event()

    def handle_signal(sig: int, frame: Any = None) -> None:
        logger.info(f"Received signal {sig}, shutting down...")
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # ── Run trading loop ──
    try:
        # Run trading loop and shutdown signal in parallel
        trading_task = asyncio.create_task(runner.run())
        shutdown_task = asyncio.create_task(shutdown_event.wait())

        done, pending = await asyncio.wait(
            [trading_task, shutdown_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Cancel pending tasks
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    except Exception as e:
        logger.error(f"Trading loop error: {e}")

    finally:
        await runner.shutdown(close_positions=args.close_on_exit)


if __name__ == "__main__":
    asyncio.run(main())
