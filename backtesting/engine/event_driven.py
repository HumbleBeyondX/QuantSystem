"""Event-driven backtest engine.

Replays historical data through the same StrategyBase interface used in live trading.
Flow: Historical Data -> on_bar/on_tick -> generate_signals -> risk check -> paper fill
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from backtesting.metrics.performance import compute_metrics
from core.enums import AssetClass, OrderSide, OrderType
from core.logging import get_logger
from core.models import Bar, Fill, PortfolioSnapshot
from execution.engine.paper_engine import PaperEngine
from execution.models.fees import FeeModel, PercentageFee
from execution.models.slippage import SlippageModel, FixedSlippage
from execution.oms.fill_tracker import FillTracker
from execution.oms.order_manager import OrderManager
from portfolio.position.position_manager import PositionManager
from portfolio.risk.risk_manager import RiskManager
from strategies.base import StrategyBase

logger = get_logger(__name__)


class EventDrivenBacktest:
    """Event-driven backtesting engine.

    Uses the exact same strategy interface as live trading,
    ensuring consistent behavior between backtest and production.
    """

    def __init__(
        self,
        strategy: StrategyBase,
        initial_cash: float = 100_000,
        slippage: Optional[SlippageModel] = None,
        fees: Optional[FeeModel] = None,
        risk_check: bool = True,
    ) -> None:
        self.strategy = strategy
        self.initial_cash = initial_cash
        self.risk_check = risk_check

        # Components
        self._position_manager = PositionManager(initial_cash)
        self._order_manager = OrderManager()
        self._paper_engine = PaperEngine(
            slippage_model=slippage or FixedSlippage(bps=5),
            fee_model=fees or PercentageFee(rate=0.001),
        )
        self._fill_tracker = FillTracker(self._position_manager)
        self._risk_manager = RiskManager() if risk_check else None

        # Results tracking
        self._equity_curve: List[Dict[str, Any]] = []
        self._trades: List[Dict[str, Any]] = []
        self._signals_log: List[Dict[str, Any]] = []

    async def run(self, data: Dict[str, pd.DataFrame]) -> Dict[str, Any]:
        """Run backtest on historical data.

        Args:
            data: {symbol: DataFrame with OHLCV columns}

        Returns:
            Dict with equity_curve, trades, metrics, signals
        """
        logger.info("backtest_starting", strategy=self.strategy.strategy_id, symbols=list(data.keys()))

        await self._paper_engine.connect()
        self.strategy.initialize()

        # Align all DataFrames by timestamp
        all_timestamps = set()
        for df in data.values():
            ts_col = self._find_timestamp_col(df)
            if ts_col:
                all_timestamps.update(df[ts_col].tolist())

        timestamps = sorted(all_timestamps)

        # Replay bars in chronological order
        for ts in timestamps:
            for symbol, df in data.items():
                ts_col = self._find_timestamp_col(df)
                if not ts_col:
                    continue

                row = df[df[ts_col] == ts]
                if row.empty:
                    continue

                r = row.iloc[0]
                bar = Bar(
                    symbol=symbol,
                    timestamp=ts if isinstance(ts, datetime) else datetime.now(),
                    open=float(r.get("open", 0)),
                    high=float(r.get("high", 0)),
                    low=float(r.get("low", 0)),
                    close=float(r.get("close", 0)),
                    volume=float(r.get("volume", 0)),
                    source="backtest",
                )

                # Update paper engine price
                self._paper_engine.update_price(symbol, bar.close)

                # Feed to strategy
                self.strategy.on_bar(symbol, bar)

            # Generate signals and execute
            targets = self.strategy.calculate_target_positions()

            if targets:
                current_positions = {
                    p.symbol: p.quantity * (1 if p.side.value == "LONG" else -1)
                    for p in self._position_manager.get_all_positions()
                }
                prices = {s: self._paper_engine._prices.get(s, 0) for s in data.keys()}

                orders = self._order_manager.create_orders_from_targets(
                    targets, current_positions, prices
                )

                for order in orders:
                    # Risk check
                    if self._risk_manager:
                        snapshot = self._position_manager.get_snapshot()
                        approved, reason = self._risk_manager.check_order(order, snapshot)
                        if not approved:
                            logger.debug("order_rejected", reason=reason)
                            continue

                    broker_id = await self._paper_engine.submit_order(order)

                # Process fills
                fills = await self._paper_engine.get_fills()
                self._fill_tracker.process_fills(fills)

                # Notify strategy
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

            # Update prices and record equity
            prices_dict = {s: self._paper_engine._prices.get(s, 0) for s in data.keys()}
            self._position_manager.update_prices(prices_dict)
            snapshot = self._position_manager.get_snapshot()

            self._equity_curve.append({
                "timestamp": str(ts),
                "total_value": snapshot.total_value,
                "cash": snapshot.cash,
                "unrealized_pnl": snapshot.unrealized_pnl,
                "n_positions": len(snapshot.positions),
            })

        # Compute metrics
        self.strategy.shutdown()
        await self._paper_engine.disconnect()

        equity_df = pd.DataFrame(self._equity_curve)
        metrics = compute_metrics(equity_df) if not equity_df.empty else {}

        result = {
            "strategy_id": self.strategy.strategy_id,
            "initial_cash": self.initial_cash,
            "equity_curve": self._equity_curve,
            "trades": self._trades,
            "metrics": metrics,
            "n_trades": len(self._trades),
            "final_value": self._equity_curve[-1]["total_value"] if self._equity_curve else self.initial_cash,
        }

        logger.info("backtest_complete", **{k: v for k, v in result.items() if k != "equity_curve" and k != "trades"})
        return result

    @staticmethod
    def _find_timestamp_col(df: pd.DataFrame) -> Optional[str]:
        for col in ("timestamp", "time", "datetime", "date"):
            if col in df.columns:
                return col
        return None
