"""BTC/ETH Macro Cycle Entry Signal Strategy.

Leveraged speculative strategy based on multi-factor macro cycle analysis.
Uses price-derived proxy signals that approximate the user's research framework:

Bullish Signals:
- Near/below 200W MA (accumulation zone)
- Pi Cycle indicator far from top
- Triple EMA alignment (21/55/200)
- Positive momentum (ROC + RSI)
- OBV accumulation pattern
- Deep drawdown from ATH (contrarian)
- Low volatility coiling (breakout imminent)

Risk Controls for 10x Leverage:
- Max position: configurable % of equity
- ATR-based stop loss: 1.5-2.5x ATR
- Take profit: 3-6x ATR
- Trailing stop after initial profit target hit
- Max drawdown circuit breaker: 15% equity
- Cooldown between trades: configurable bars
- Max trades per day: 3

Designed for 4H timeframe, ~1-3 trades per day when conditions align.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, List, Optional
from uuid import uuid4

import numpy as np

from core.enums import SignalDirection
from core.models import Bar, Signal, TargetPosition
from data.features.macro_signals import (
    MacroSignalState,
    compute_atr,
    compute_composite_score,
    update_state,
)
from strategies.base import StrategyBase, StrategyConfig
from strategies.registry import register_strategy


@register_strategy("crypto_macro_cycle")
class MacroCycleStrategy(StrategyBase):
    """10x leveraged macro cycle entry strategy for BTC/ETH.

    Low-frequency (4H bars), high-conviction entries based on
    multi-factor composite signal score.
    """

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

        # ── Signal Parameters ──
        self.entry_threshold = self.get_param("entry_threshold", 0.25)
        self.exit_threshold = self.get_param("exit_threshold", -0.10)
        self.strong_entry_threshold = self.get_param("strong_entry_threshold", 0.45)
        self.short_entry_threshold = self.get_param("short_entry_threshold", -0.25)
        self.short_exit_threshold = self.get_param("short_exit_threshold", 0.10)

        # Signal weights (override defaults)
        self.signal_weights = self.get_param("signal_weights", {
            "ma200w":     0.15,
            "pi_cycle":   0.10,
            "trend":      0.25,
            "momentum":   0.20,
            "volume":     0.10,
            "drawdown":   0.10,
            "volatility": 0.10,
        })

        # ── Leverage & Position Sizing ──
        self.leverage = self.get_param("leverage", 10.0)
        self.base_position_pct = self.get_param("base_position_pct", 0.10)  # 10% equity per trade
        self.max_position_pct = self.get_param("max_position_pct", 0.25)     # 25% max equity risk

        # ── Risk Management ──
        self.atr_period = self.get_param("atr_period", 14)
        self.atr_stop_mult = self.get_param("atr_stop_mult", 1.5)
        self.atr_tp_mult = self.get_param("atr_tp_mult", 3.0)
        self.trailing_stop_atr = self.get_param("trailing_stop_atr", 1.5)
        self.max_drawdown_pct = self.get_param("max_drawdown_pct", 0.15)  # 15% max equity DD
        self.cooldown_bars = self.get_param("cooldown_bars", 3)  # Min bars between trades
        self.max_trades_per_day = self.get_param("max_trades_per_day", 3)

        # ── Initial Capital (for position sizing) ──
        self.initial_capital = self.get_param("initial_capital", 10000.0)

        # ── Per-symbol State ──
        self._signal_state: Dict[str, MacroSignalState] = {}
        self._highest_since_entry: Dict[str, float] = {}
        self._lowest_since_entry: Dict[str, float] = {}
        self._entry_price: Dict[str, float] = {}
        self._bars_since_trade: Dict[str, int] = {}
        self._daily_trades: Dict[str, int] = {}
        self._last_trade_day: Dict[str, str] = {}

        # ── Equity Tracking ──
        self._peak_equity: float = self.initial_capital
        self._current_equity: float = self.initial_capital
        self._realized_pnl: float = 0.0
        self._circuit_breaker_active: bool = False

        # ── Current bar time ──
        self._current_time: datetime = datetime.utcnow()
        self._bar_count: int = 0

    def initialize(self) -> None:
        super().initialize()
        self._peak_equity = self.initial_capital
        self._current_equity = self.initial_capital

    def on_bar(self, symbol: str, bar: Bar) -> None:
        """Process each 4H bar: update macro signal state."""
        super().on_bar(symbol, bar)
        self._current_time = bar.timestamp
        self._bar_count += 1

        # Initialize per-symbol state
        if symbol not in self._signal_state:
            self._signal_state[symbol] = MacroSignalState()
            self._bars_since_trade[symbol] = self.cooldown_bars + 1  # Allow first trade

        # Update macro signal state
        update_state(
            self._signal_state[symbol],
            close=bar.close,
            high=bar.high,
            low=bar.low,
            volume=bar.volume,
        )

        # Increment cooldown counter
        self._bars_since_trade[symbol] = self._bars_since_trade.get(symbol, 0) + 1

        # Track trailing stop levels
        if not self.is_flat(symbol):
            pos = self.get_position(symbol)
            if pos > 0:
                self._highest_since_entry[symbol] = max(
                    self._highest_since_entry.get(symbol, bar.close), bar.close
                )
            elif pos < 0:
                self._lowest_since_entry[symbol] = min(
                    self._lowest_since_entry.get(symbol, bar.close), bar.close
                )

        # Update equity estimate (rough: based on unrealized P&L)
        if not self.is_flat(symbol) and symbol in self._entry_price:
            entry = self._entry_price[symbol]
            pos = self.get_position(symbol)
            unrealized = (bar.close - entry) * pos
            self._current_equity = self.initial_capital + self._realized_pnl + unrealized
        else:
            self._current_equity = self.initial_capital + self._realized_pnl

        # Peak equity tracking
        if self._current_equity > self._peak_equity:
            self._peak_equity = self._current_equity

        # Circuit breaker check with auto-reset after cooldown
        if self._peak_equity > 0:
            dd = (self._peak_equity - self._current_equity) / self._peak_equity
            if self._circuit_breaker_active:
                # Count cooldown bars
                self._cb_bars = getattr(self, '_cb_bars', 0) + 1
                if self._cb_bars >= 48 and self.is_flat(symbol):
                    # Cooldown expired (48 bars ≈ 8 days on 4H) + flat → resume
                    self._circuit_breaker_active = False
                    self._peak_equity = self._current_equity
            elif dd > self.max_drawdown_pct:
                self._circuit_breaker_active = True
                self._cb_bars = 0

    def on_fill(self, fill) -> None:
        """Track realized P&L on fills."""
        super().on_fill(fill)

        symbol = fill.symbol
        # Track entry price
        pos = self.get_position(symbol)
        if abs(pos) > 1e-10 and symbol not in self._entry_price:
            self._entry_price[symbol] = fill.price
        elif abs(pos) < 1e-10 and symbol in self._entry_price:
            # Position closed — calculate realized P&L
            entry = self._entry_price.pop(symbol, fill.price)
            pnl = (fill.price - entry) * fill.quantity
            if fill.side.value == "SELL":
                pnl = (fill.price - entry) * fill.quantity
            else:
                pnl = (entry - fill.price) * fill.quantity
            self._realized_pnl += pnl - fill.commission

    def generate_signals(self) -> List[Signal]:
        """Generate macro cycle entry/exit signals."""
        signals: List[Signal] = []
        now = self._current_time

        for symbol in self.symbols:
            state = self._signal_state.get(symbol)
            if not state or not state.initialized:
                continue

            price = state.closes[-1]
            atr = compute_atr(state, self.atr_period)
            if atr == 0 or price == 0:
                continue

            # Compute composite macro score
            composite, individual_scores = compute_composite_score(
                state, self.signal_weights
            )

            # ── Daily trade limit check ──
            day_key = now.strftime("%Y-%m-%d") if hasattr(now, 'strftime') else str(now)[:10]
            if self._last_trade_day.get(symbol) != day_key:
                self._daily_trades[symbol] = 0
                self._last_trade_day[symbol] = day_key

            # ── Circuit breaker ──
            if self._circuit_breaker_active and self.is_flat(symbol):
                # Don't open new positions when circuit breaker active
                continue

            direction = None
            strength = 0.0

            if self.is_flat(symbol):
                # ═══ ENTRY LOGIC ═══

                # Check cooldown
                if self._bars_since_trade.get(symbol, 0) < self.cooldown_bars:
                    continue

                # Check daily trade limit
                if self._daily_trades.get(symbol, 0) >= self.max_trades_per_day:
                    continue

                # LONG entry
                if composite >= self.entry_threshold:
                    direction = SignalDirection.LONG

                    # Scale strength by how far above threshold
                    excess = composite - self.entry_threshold
                    max_excess = 1.0 - self.entry_threshold
                    strength = min(0.3 + 0.7 * (excess / max_excess), 1.0)

                    # Strong signal bonus
                    if composite >= self.strong_entry_threshold:
                        strength = min(strength + 0.2, 1.0)

                # SHORT entry
                elif composite <= self.short_entry_threshold:
                    direction = SignalDirection.SHORT

                    excess = abs(composite) - abs(self.short_entry_threshold)
                    max_excess = 1.0 - abs(self.short_entry_threshold)
                    strength = min(0.3 + 0.7 * (excess / max_excess), 1.0)

            else:
                # ═══ EXIT LOGIC ═══
                pos = self.get_position(symbol)

                if pos > 0:
                    # Long position exit checks
                    high = self._highest_since_entry.get(symbol, price)
                    trailing_stop = high - self.trailing_stop_atr * atr

                    # 1. Trailing stop hit
                    hit_trailing = price < trailing_stop

                    # 2. Signal reversal
                    signal_reversal = composite < self.exit_threshold

                    # 3. Circuit breaker
                    force_exit = self._circuit_breaker_active

                    # 4. Fixed stop loss from entry
                    entry = self._entry_price.get(symbol, price)
                    fixed_stop = entry - self.atr_stop_mult * atr
                    hit_fixed_stop = price < fixed_stop

                    # 5. Take profit
                    tp_level = entry + self.atr_tp_mult * atr
                    hit_tp = price >= tp_level

                    if hit_trailing or signal_reversal or force_exit or hit_fixed_stop:
                        direction = SignalDirection.FLAT
                        strength = 1.0
                    elif hit_tp:
                        # Partial exit or full exit at take profit
                        direction = SignalDirection.FLAT
                        strength = 0.8

                elif pos < 0:
                    # Short position exit checks
                    low = self._lowest_since_entry.get(symbol, price)
                    trailing_stop = low + self.trailing_stop_atr * atr

                    hit_trailing = price > trailing_stop
                    signal_reversal = composite > self.short_exit_threshold
                    force_exit = self._circuit_breaker_active

                    entry = self._entry_price.get(symbol, price)
                    fixed_stop = entry + self.atr_stop_mult * atr
                    hit_fixed_stop = price > fixed_stop

                    tp_level = entry - self.atr_tp_mult * atr
                    hit_tp = price <= tp_level

                    if hit_trailing or signal_reversal or force_exit or hit_fixed_stop:
                        direction = SignalDirection.FLAT
                        strength = 1.0
                    elif hit_tp:
                        direction = SignalDirection.FLAT
                        strength = 0.8

            # ── Build Signal ──
            if direction is not None:
                sl = tp = None
                if direction == SignalDirection.LONG:
                    sl = price - self.atr_stop_mult * atr
                    tp = price + self.atr_tp_mult * atr
                    self._highest_since_entry[symbol] = price
                    self._bars_since_trade[symbol] = 0
                    self._daily_trades[symbol] = self._daily_trades.get(symbol, 0) + 1
                elif direction == SignalDirection.SHORT:
                    sl = price + self.atr_stop_mult * atr
                    tp = price - self.atr_tp_mult * atr
                    self._lowest_since_entry[symbol] = price
                    self._bars_since_trade[symbol] = 0
                    self._daily_trades[symbol] = self._daily_trades.get(symbol, 0) + 1
                else:
                    # Exiting position — reset tracking
                    self._highest_since_entry.pop(symbol, None)
                    self._lowest_since_entry.pop(symbol, None)
                    self._bars_since_trade[symbol] = 0

                signals.append(Signal(
                    signal_id=uuid4(),
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    direction=direction,
                    strength=strength,
                    timestamp=now,
                    target_price=price,
                    stop_loss=sl,
                    take_profit=tp,
                    metadata={
                        "composite_score": round(composite, 4),
                        **{f"sig_{k}": round(v, 4) for k, v in individual_scores.items()},
                        "atr": round(atr, 2),
                        "leverage": self.leverage,
                        "circuit_breaker": self._circuit_breaker_active,
                        "equity_dd_pct": round(
                            (self._peak_equity - self._current_equity) / max(self._peak_equity, 1) * 100, 2
                        ),
                    },
                ))

        return signals

    def calculate_target_positions(self) -> List[TargetPosition]:
        """Position sizing with leverage for macro cycle strategy.

        Uses signal strength + leverage to determine target notional.
        Base allocation: 10% of equity × leverage × strength
        """
        signals = self.generate_signals()
        self._signals.extend(signals)
        self._metrics["signals_generated"] += len(signals)

        targets = []
        for signal in signals:
            symbol = signal.symbol
            state = self._signal_state.get(symbol)
            if not state or not state.closes:
                continue

            price = state.closes[-1]
            if price <= 0:
                continue

            if signal.direction == SignalDirection.FLAT:
                targets.append(TargetPosition(
                    symbol=symbol,
                    strategy_id=self.strategy_id,
                    target_quantity=0.0,
                    target_weight=0.0,
                    urgency=0.9,  # Exit quickly
                    timestamp=signal.timestamp,
                ))
                continue

            # Position sizing: equity × position_pct × leverage × strength
            # Cap equity to prevent runaway compounding (max 5x initial)
            equity = max(self._current_equity, self.initial_capital * 0.5)
            capped_equity = min(equity, self.initial_capital * 5)
            position_pct = min(
                self.base_position_pct * signal.strength,
                self.max_position_pct,
            )
            notional_usd = capped_equity * position_pct * self.leverage
            qty = notional_usd / price

            if signal.direction == SignalDirection.SHORT:
                qty = -qty

            targets.append(TargetPosition(
                symbol=symbol,
                strategy_id=self.strategy_id,
                target_quantity=qty,
                target_weight=position_pct,
                urgency=0.5,
                timestamp=signal.timestamp,
            ))

        return targets
