"""Crypto scalping strategy: tick-level EMA + VWAP + Microprice.

Targets very short-term mean reversion around fair value using microprice
from orderbook data and EMA/VWAP deviation as entry triggers.
Typically operates on 1s-1m timeframes with tight stops.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional
from uuid import uuid4

from core.enums import SignalDirection
from core.models import Bar, OrderBookSnapshot, Signal, Tick
from strategies.base import StrategyBase, StrategyConfig
from strategies.registry import register_strategy


@register_strategy("crypto_scalping")
class ScalpingStrategy(StrategyBase):
    """Tick-level scalping using microprice + EMA + VWAP deviation."""

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

        # Parameters with defaults
        self.ema_fast_period = self.get_param("ema_fast", 9)
        self.ema_slow_period = self.get_param("ema_slow", 21)
        self.rsi_period = self.get_param("rsi_period", 14)
        self.rsi_overbought = self.get_param("rsi_overbought", 70)
        self.rsi_oversold = self.get_param("rsi_oversold", 30)
        self.take_profit_pct = self.get_param("take_profit_pct", 0.3) / 100
        self.stop_loss_pct = self.get_param("stop_loss_pct", 0.15) / 100
        self.cooldown_seconds = self.get_param("cooldown_seconds", 30)
        self.max_position_usd = self.get_param("max_position_size_usd", 10000)
        self.imbalance_threshold = self.get_param("imbalance_threshold", 0.3)

        # State
        self._prices: Dict[str, Deque[float]] = {}
        self._microprice: Dict[str, float] = {}
        self._imbalance: Dict[str, float] = {}
        self._ema_fast: Dict[str, float] = {}
        self._ema_slow: Dict[str, float] = {}
        self._last_signal_time: Dict[str, datetime] = {}
        self._pending_signals: List[Signal] = []
        self._current_time: datetime = datetime.utcnow()

    def _update_price(self, symbol: str, price: float) -> None:
        """Core price update: update EMAs from any data source (tick or bar)."""
        if price <= 0:
            return

        if symbol not in self._prices:
            self._prices[symbol] = deque(maxlen=200)
            self._ema_fast[symbol] = price
            self._ema_slow[symbol] = price

        self._prices[symbol].append(price)

        alpha_fast = 2 / (self.ema_fast_period + 1)
        alpha_slow = 2 / (self.ema_slow_period + 1)
        self._ema_fast[symbol] = alpha_fast * price + (1 - alpha_fast) * self._ema_fast[symbol]
        self._ema_slow[symbol] = alpha_slow * price + (1 - alpha_slow) * self._ema_slow[symbol]

    def on_tick(self, symbol: str, tick: Tick) -> None:
        """Process each tick for EMA updates."""
        price = tick.last or (tick.bid + tick.ask) / 2
        self._update_price(symbol, price)

    def on_bar(self, symbol: str, bar: Bar) -> None:
        """Process bar data: converts close to tick-equivalent update.

        This allows the same EMA/microprice logic to work for both
        bar-level and tick-level backtests.
        """
        super().on_bar(symbol, bar)
        self._current_time = bar.timestamp
        self._update_price(symbol, bar.close)

        # Estimate orderbook imbalance from bar price action
        bar_range = bar.high - bar.low
        if bar_range > 0:
            buy_pressure = (bar.close - bar.low) / bar_range
            self._imbalance[symbol] = (buy_pressure - 0.5) * 2  # scale to [-1, 1]
        self._microprice[symbol] = bar.close

    def on_orderbook(self, symbol: str, orderbook: OrderBookSnapshot) -> None:
        """Extract microprice and imbalance from orderbook."""
        self._microprice[symbol] = orderbook.microprice
        if orderbook.bids and orderbook.asks:
            bid_qty = orderbook.bids[0].quantity
            ask_qty = orderbook.asks[0].quantity
            total = bid_qty + ask_qty
            self._imbalance[symbol] = (bid_qty - ask_qty) / total if total > 0 else 0.0

    def calculate_target_positions(self):
        """Override: dollar-based position sizing for scalping.

        Converts signal strength into actual BTC quantities based on
        max_position_usd and current price.
        """
        from core.models import TargetPosition

        signals = self.generate_signals()
        self._signals.extend(signals)
        self._metrics["signals_generated"] += len(signals)

        targets = []
        for signal in signals:
            symbol = signal.symbol
            price = self._prices[symbol][-1] if symbol in self._prices and self._prices[symbol] else 0
            if price <= 0:
                continue

            # Dollar-based sizing: strength scales the max position
            position_usd = self.max_position_usd * signal.strength
            qty_in_asset = position_usd / price

            if signal.direction == SignalDirection.LONG:
                target_qty = qty_in_asset
            elif signal.direction == SignalDirection.SHORT:
                target_qty = -qty_in_asset
            else:
                target_qty = 0.0  # FLAT → close

            targets.append(TargetPosition(
                symbol=symbol,
                strategy_id=self.strategy_id,
                target_quantity=target_qty,
                target_weight=signal.strength,
                urgency=0.8,
                timestamp=signal.timestamp,
            ))

        return targets

    def generate_signals(self) -> List[Signal]:
        """Generate scalping signals based on microprice deviation + orderbook imbalance."""
        signals = self._pending_signals.copy()
        self._pending_signals.clear()

        now = self._current_time

        for symbol in self.symbols:
            # Check cooldown
            last_time = self._last_signal_time.get(symbol)
            if last_time and (now - last_time).total_seconds() < self.cooldown_seconds:
                continue

            if symbol not in self._prices or len(self._prices[symbol]) < self.ema_slow_period:
                continue

            price = self._prices[symbol][-1]
            ema_fast = self._ema_fast.get(symbol, price)
            ema_slow = self._ema_slow.get(symbol, price)
            microprice = self._microprice.get(symbol, price)
            imbalance = self._imbalance.get(symbol, 0.0)

            # Scalping logic:
            # LONG: microprice > ema_fast > ema_slow AND orderbook buy pressure
            # SHORT: microprice < ema_fast < ema_slow AND orderbook sell pressure
            direction = None
            strength = 0.0

            if (microprice > ema_fast > ema_slow
                    and imbalance > self.imbalance_threshold
                    and self.is_flat(symbol)):
                direction = SignalDirection.LONG
                # Strength based on imbalance magnitude
                strength = min(abs(imbalance), 1.0)

            elif (microprice < ema_fast < ema_slow
                    and imbalance < -self.imbalance_threshold
                    and self.is_flat(symbol)):
                direction = SignalDirection.SHORT
                strength = min(abs(imbalance), 1.0)

            # Exit signals: reversion to mean
            elif not self.is_flat(symbol):
                pos = self.get_position(symbol)
                if pos > 0 and microprice < ema_fast:
                    direction = SignalDirection.FLAT
                    strength = 0.8
                elif pos < 0 and microprice > ema_fast:
                    direction = SignalDirection.FLAT
                    strength = 0.8

            if direction is not None:
                tp = price * (1 + self.take_profit_pct) if direction == SignalDirection.LONG else price * (1 - self.take_profit_pct)
                sl = price * (1 - self.stop_loss_pct) if direction == SignalDirection.LONG else price * (1 + self.stop_loss_pct)

                signals.append(Signal(
                    signal_id=uuid4(),
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=direction,
                    strength=strength,
                    timestamp=now,
                    target_price=microprice,
                    take_profit=tp if direction != SignalDirection.FLAT else None,
                    stop_loss=sl if direction != SignalDirection.FLAT else None,
                    metadata={
                        "ema_fast": ema_fast,
                        "ema_slow": ema_slow,
                        "microprice": microprice,
                        "imbalance": imbalance,
                    },
                ))
                self._last_signal_time[symbol] = now

        return signals
