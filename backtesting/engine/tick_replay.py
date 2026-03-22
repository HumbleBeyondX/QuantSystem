"""Tick-level backtest engine for scalping strategies.

Replays trade-by-trade or tick-by-tick data through strategies that use
on_tick() and on_orderbook() instead of on_bar().

Supports:
- Real trade data (from ccxt fetch_trades)
- Tick data with bid/ask/last/volume
- Synthetic orderbook reconstruction from tick imbalance
- Configurable signal evaluation frequency (every N ticks)
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from backtesting.metrics.performance import compute_metrics
from core.enums import AssetClass, OrderSide, OrderType, SignalDirection
from core.logging import get_logger
from core.models import (
    Bar,
    Fill,
    OrderBookLevel,
    OrderBookSnapshot,
    PortfolioSnapshot,
    Tick,
)
from execution.engine.paper_engine import PaperEngine
from execution.models.fees import CryptoMakerTakerFee, FeeModel
from execution.models.slippage import FixedSlippage, SlippageModel
from execution.oms.fill_tracker import FillTracker
from execution.oms.order_manager import OrderManager
from portfolio.position.position_manager import PositionManager
from strategies.base import StrategyBase

logger = get_logger(__name__)


class TickBacktestEngine:
    """Tick-level backtesting engine for scalping strategies.

    Replays tick data through strategy.on_tick() and strategy.on_orderbook(),
    then evaluates signals every `eval_every_n` ticks.
    """

    def __init__(
        self,
        strategy: StrategyBase,
        initial_cash: float = 100_000,
        slippage: Optional[SlippageModel] = None,
        fees: Optional[FeeModel] = None,
        eval_every_n: int = 10,
        snapshot_every_n: int = 100,
    ) -> None:
        """
        Args:
            strategy: Strategy instance (must implement on_tick/on_orderbook)
            initial_cash: Starting capital in USD
            slippage: Slippage model (default: 2 bps for crypto scalping)
            fees: Fee model (default: maker 0.02% / taker 0.05%)
            eval_every_n: Evaluate signals every N ticks
            snapshot_every_n: Record equity snapshot every N ticks
        """
        self.strategy = strategy
        self.initial_cash = initial_cash
        self.eval_every_n = eval_every_n
        self.snapshot_every_n = snapshot_every_n

        # Execution components
        self._position_manager = PositionManager(initial_cash)
        self._order_manager = OrderManager()
        self._paper_engine = PaperEngine(
            slippage_model=slippage or FixedSlippage(bps=2),
            fee_model=fees or CryptoMakerTakerFee(maker=0.0002, taker=0.0005),
            fill_delay_ms=0,  # no delay in backtest
        )
        self._fill_tracker = FillTracker(self._position_manager)

        # Results tracking
        self._equity_curve: List[Dict[str, Any]] = []
        self._trades: List[Dict[str, Any]] = []
        self._signals_log: List[Dict[str, Any]] = []
        self._tick_count = 0

    async def run(self, data: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
        """Run tick-level backtest.

        Args:
            data: {symbol: DataFrame} where DataFrame has columns:
                For tick data: timestamp, bid, ask, last, volume [, microprice, imbalance, bid_qty_1..5, ask_qty_1..5]
                For trade data: timestamp, price, amount, side [, cost]

        Returns:
            Dict with equity_curve, trades, metrics, signals
        """
        logger.info(
            "tick_backtest_starting",
            strategy=self.strategy.strategy_id,
            symbols=list(data.keys()),
        )

        await self._paper_engine.connect()
        self.strategy.initialize()

        # Detect data format and normalize
        normalized = {}
        for symbol, df in data.items():
            normalized[symbol] = self._normalize_data(df)

        # Interleave all symbols by timestamp for chronological replay
        timeline = self._build_timeline(normalized)

        total_ticks = len(timeline)
        logger.info("Replaying %d ticks across %d symbols", total_ticks, len(data))

        for idx, (ts, symbol, row) in enumerate(timeline):
            self._tick_count += 1

            # Create Tick object
            tick = self._row_to_tick(symbol, ts, row)
            price = tick.last or (tick.bid + tick.ask) / 2

            # Update paper engine price
            if price > 0:
                self._paper_engine.update_price(symbol, price)

            # Feed tick to strategy
            self.strategy.on_tick(symbol, tick)

            # Feed orderbook if available
            orderbook = self._row_to_orderbook(symbol, ts, row)
            if orderbook:
                self.strategy.on_orderbook(symbol, orderbook)

            # Evaluate signals every N ticks
            if self._tick_count % self.eval_every_n == 0:
                await self._evaluate_and_execute(data.keys())

            # Record equity snapshot
            if self._tick_count % self.snapshot_every_n == 0:
                self._record_snapshot(ts, data.keys())

            # Progress logging
            if self._tick_count % 10000 == 0:
                pct = (idx + 1) / total_ticks * 100
                snapshot = self._position_manager.get_snapshot()
                logger.info(
                    "tick_progress",
                    pct=f"{pct:.1f}%",
                    ticks=self._tick_count,
                    value=f"${snapshot.total_value:,.2f}",
                    trades=len(self._trades),
                )

        # Final evaluation and snapshot
        await self._evaluate_and_execute(data.keys())
        if timeline:
            self._record_snapshot(timeline[-1][0], data.keys())

        # Shutdown
        self.strategy.shutdown()
        await self._paper_engine.disconnect()

        # Compute metrics
        equity_df = pd.DataFrame(self._equity_curve)
        metrics = compute_metrics(equity_df) if len(equity_df) > 1 else {}

        result = {
            "strategy_id": self.strategy.strategy_id,
            "initial_cash": self.initial_cash,
            "equity_curve": self._equity_curve,
            "trades": self._trades,
            "signals": self._signals_log,
            "metrics": metrics,
            "n_trades": len(self._trades),
            "n_ticks": self._tick_count,
            "final_value": self._equity_curve[-1]["total_value"] if self._equity_curve else self.initial_cash,
        }

        logger.info(
            "tick_backtest_complete",
            n_ticks=self._tick_count,
            n_trades=len(self._trades),
            final_value=result["final_value"],
            total_return=metrics.get("total_return_pct", 0),
            sharpe=metrics.get("sharpe_ratio", 0),
        )

        return result

    # ------------------------------------------------------------------
    # Internal: Data normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_data(df: pd.DataFrame) -> pd.DataFrame:
        """Normalize tick or trade data to a common format."""
        df = df.copy()

        # Ensure timestamp column exists and is datetime
        if "timestamp" not in df.columns:
            for col in ("time", "datetime", "date"):
                if col in df.columns:
                    df.rename(columns={col: "timestamp"}, inplace=True)
                    break

        if "timestamp" in df.columns and not pd.api.types.is_datetime64_any_dtype(df["timestamp"]):
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

        # Trade data format → tick format
        if "price" in df.columns and "bid" not in df.columns:
            df["last"] = df["price"]
            # Estimate bid/ask from price
            spread_estimate = df["price"].rolling(100).std().fillna(df["price"].std()) * 0.1
            spread_estimate = spread_estimate.clip(lower=df["price"].median() * 0.00005)
            df["bid"] = df["price"] - spread_estimate / 2
            df["ask"] = df["price"] + spread_estimate / 2
            if "amount" in df.columns:
                df["volume"] = df["amount"]

        # Fill defaults
        for col in ("bid", "ask", "last", "volume"):
            if col not in df.columns:
                df[col] = 0.0

        return df.sort_values("timestamp").reset_index(drop=True)

    def _build_timeline(
        self, data: Dict[str, pd.DataFrame]
    ) -> List[tuple]:
        """Build interleaved timeline of (timestamp, symbol, row) tuples."""
        events = []
        for symbol, df in data.items():
            for _, row in df.iterrows():
                events.append((row["timestamp"], symbol, row))

        events.sort(key=lambda x: x[0])
        return events

    @staticmethod
    def _row_to_tick(symbol: str, ts, row) -> Tick:
        """Convert DataFrame row to Tick object."""
        return Tick(
            symbol=symbol,
            timestamp=ts if isinstance(ts, datetime) else datetime.utcnow(),
            bid=float(row.get("bid", 0)),
            ask=float(row.get("ask", 0)),
            last=float(row.get("last", 0)),
            volume=float(row.get("volume", 0)),
            source="backtest",
            asset_class=AssetClass.CRYPTO,
        )

    @staticmethod
    def _row_to_orderbook(symbol: str, ts, row) -> Optional[OrderBookSnapshot]:
        """Reconstruct orderbook from row data (if depth columns available)."""
        bid_price = float(row.get("bid", 0))
        ask_price = float(row.get("ask", 0))

        if bid_price <= 0 or ask_price <= 0:
            return None

        spread = ask_price - bid_price
        tick_size = max(spread * 0.3, 0.01)

        bids = []
        asks = []

        for level in range(5):
            bid_qty_col = f"bid_qty_{level + 1}"
            ask_qty_col = f"ask_qty_{level + 1}"

            bq = float(row.get(bid_qty_col, max(1.0, np.random.exponential(3.0))))
            aq = float(row.get(ask_qty_col, max(1.0, np.random.exponential(3.0))))

            bids.append(OrderBookLevel(price=bid_price - level * tick_size, quantity=bq))
            asks.append(OrderBookLevel(price=ask_price + level * tick_size, quantity=aq))

        return OrderBookSnapshot(
            symbol=symbol,
            timestamp=ts if isinstance(ts, datetime) else datetime.utcnow(),
            bids=bids,
            asks=asks,
            source="backtest",
        )

    # ------------------------------------------------------------------
    # Internal: Signal evaluation and execution
    # ------------------------------------------------------------------

    async def _evaluate_and_execute(self, symbols):
        """Evaluate strategy signals and execute orders."""
        targets = self.strategy.calculate_target_positions()

        if not targets:
            return

        current_positions = {
            p.symbol: p.quantity * (1 if p.side.value == "LONG" else -1)
            for p in self._position_manager.get_all_positions()
        }
        prices = {s: self._paper_engine._prices.get(s, 0) for s in symbols}

        orders = self._order_manager.create_orders_from_targets(
            targets, current_positions, prices,
        )

        for order in orders:
            await self._paper_engine.submit_order(order)

        fills = await self._paper_engine.get_fills()
        self._fill_tracker.process_fills(fills)

        for fill in fills:
            self.strategy.on_fill(fill)
            self._trades.append({
                "timestamp": str(fill.timestamp),
                "symbol": fill.symbol,
                "side": fill.side.value,
                "price": fill.price,
                "quantity": fill.quantity,
                "commission": fill.commission,
                "strategy_id": fill.strategy_id,
            })

        # Log signals
        for target in targets:
            self._signals_log.append({
                "timestamp": str(target.timestamp),
                "symbol": target.symbol,
                "target_qty": target.target_quantity,
                "target_weight": target.target_weight,
            })

    def _record_snapshot(self, ts, symbols):
        """Record equity curve snapshot."""
        prices = {s: self._paper_engine._prices.get(s, 0) for s in symbols}
        self._position_manager.update_prices(prices)
        snapshot = self._position_manager.get_snapshot()

        self._equity_curve.append({
            "timestamp": str(ts),
            "total_value": snapshot.total_value,
            "cash": snapshot.cash,
            "unrealized_pnl": snapshot.unrealized_pnl,
            "n_positions": len(snapshot.positions),
            "tick_count": self._tick_count,
        })
