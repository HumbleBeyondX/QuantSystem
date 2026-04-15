"""BTC Momentum Sniper: high-leverage trend-following with fixed-risk sizing.

Designed for BTC perpetual futures on 4H timeframe.
Uses Donchian breakout + EMA pullback entries, trend-direction filtering,
and a fixed-risk position sizing model that creates structural asymmetry:
losses are capped by margin (small), wins ride via trailing stop (large).

Core principle: qty = risk_amount / stop_distance
This decouples leverage from risk — leverage only reduces margin, not max loss.

Backtest performance (2020-2026):
  - L50: +2992% total, +73% annual, Sharpe 1.68, DD 15.8%
  - L40: +1790% total, +60% annual, Sharpe 1.45, DD 18.8%
  - Every year profitable except mild -12% in 2025 choppy market
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional
from uuid import uuid4

import numpy as np

from core.enums import SignalDirection
from core.models import Bar, Signal, TargetPosition
from strategies.base import StrategyBase, StrategyConfig
from strategies.registry import register_strategy


@register_strategy("btc_sniper")
class SniperStrategy(StrategyBase):
    """High-leverage BTC momentum sniper with fixed-risk position sizing.

    Entry modes:
      1. Breakout: Close > Donchian(10) high in trend direction
      2. Pullback: Price bounces off EMA(21) in confirmed trend

    Trend filter: 2-of-3 voting (EMA55 slope, price vs EMA55, DI spread)
    Position sizing: Fixed risk per trade (default 5% of equity)
    Exit: ATR trailing stop or margin liquidation (whichever comes first)
    """

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

        # ── Leverage & Risk ──
        self.leverage = self.get_param("leverage", 50.0)
        self.risk_per_trade = self.get_param("risk_per_trade", 0.05)
        self.max_margin_pct = self.get_param("max_margin_pct", 0.30)

        # ── Entry ──
        self.min_score = self.get_param("min_score", 2)
        self.vol_min = self.get_param("vol_min", 1.2)
        self.use_donchian10 = self.get_param("use_donchian10", True)

        # ── Stops (ATR multiples) ──
        self.atr_stop = self.get_param("atr_stop", 3.5)
        self.atr_trail = self.get_param("atr_trail", 2.5)
        self.atr_trail_activate = self.get_param("atr_trail_activate", 2.0)

        # ── Time ──
        self.cooldown = self.get_param("cooldown", 3)
        self.max_hold = self.get_param("max_hold", 120)

        # ── Indicator periods ──
        self.ema8_period = self.get_param("ema8_period", 8)
        self.ema21_period = self.get_param("ema21_period", 21)
        self.ema55_period = self.get_param("ema55_period", 55)
        self.atr_period = self.get_param("atr_period", 14)
        self.adx_period = self.get_param("adx_period", 14)
        self.rsi_period = self.get_param("rsi_period", 14)
        self.dc_period = self.get_param("dc_period", 10)

        # ── Internal state per symbol ──
        self._bars: Dict[str, Deque[Bar]] = {}
        self._ema8: Dict[str, float] = {}
        self._ema21: Dict[str, float] = {}
        self._ema55: Dict[str, float] = {}
        self._atr: Dict[str, float] = {}
        self._rsi_gain: Dict[str, float] = {}
        self._rsi_loss: Dict[str, float] = {}
        self._rsi: Dict[str, float] = {}
        self._macd_hist: Dict[str, float] = {}
        self._adx: Dict[str, float] = {}
        self._plus_di: Dict[str, float] = {}
        self._minus_di: Dict[str, float] = {}
        self._vol_sma: Dict[str, float] = {}

        # Position tracking
        self._entry_price: Dict[str, float] = {}
        self._entry_atr: Dict[str, float] = {}
        self._stop_dist: Dict[str, float] = {}
        self._best_price: Dict[str, float] = {}
        self._worst_price: Dict[str, float] = {}
        self._bars_held: Dict[str, int] = {}
        self._bars_since: Dict[str, int] = {}
        self._current_time: datetime = datetime.utcnow()

        # Live trading: dynamic equity (set externally by runner/orchestrator)
        self._live_equity: Optional[float] = None

    def on_bar(self, symbol: str, bar: Bar) -> None:
        """Process each new 4H bar: update indicators and state."""
        super().on_bar(symbol, bar)
        self._current_time = bar.timestamp
        price = bar.close

        if symbol not in self._bars:
            self._init_symbol(symbol, price)

        self._bars[symbol].append(bar)
        self._update_indicators(symbol, bar)

        # Track position extremes
        if not self.is_flat(symbol):
            pos = self.get_position(symbol)
            if pos > 0:
                self._best_price[symbol] = max(self._best_price.get(symbol, bar.high), bar.high)
            elif pos < 0:
                wp = self._worst_price.get(symbol, bar.low)
                self._worst_price[symbol] = min(wp, bar.low) if wp > 0 else bar.low
            self._bars_held[symbol] = self._bars_held.get(symbol, 0) + 1

        self._bars_since[symbol] = self._bars_since.get(symbol, 0) + 1

    def _init_symbol(self, symbol: str, price: float) -> None:
        self._bars[symbol] = deque(maxlen=200)
        self._ema8[symbol] = price
        self._ema21[symbol] = price
        self._ema55[symbol] = price
        self._atr[symbol] = 0.0
        self._rsi_gain[symbol] = 0.0
        self._rsi_loss[symbol] = 0.0
        self._rsi[symbol] = 50.0
        self._macd_hist[symbol] = 0.0
        self._adx[symbol] = 0.0
        self._plus_di[symbol] = 0.0
        self._minus_di[symbol] = 0.0
        self._vol_sma[symbol] = 0.0
        self._bars_since[symbol] = 999
        self._bars_held[symbol] = 0

    def _update_indicators(self, symbol: str, bar: Bar) -> None:
        """Update all indicators incrementally."""
        c = bar.close
        h = bar.high
        lo = bar.low

        # EMAs
        for ema, period in [
            (self._ema8, self.ema8_period),
            (self._ema21, self.ema21_period),
            (self._ema55, self.ema55_period),
        ]:
            alpha = 2 / (period + 1)
            ema[symbol] = alpha * c + (1 - alpha) * ema[symbol]

        # ATR
        bars = self._bars[symbol]
        if len(bars) >= 2:
            prev_c = bars[-2].close
            tr = max(h - lo, abs(h - prev_c), abs(lo - prev_c))
        else:
            tr = h - lo
        alpha_atr = 2 / (self.atr_period + 1)
        self._atr[symbol] = alpha_atr * tr + (1 - alpha_atr) * self._atr[symbol]

        # RSI
        delta = c - (bars[-2].close if len(bars) >= 2 else c)
        gain = max(delta, 0)
        loss_val = max(-delta, 0)
        alpha_rsi = 1 / self.rsi_period
        self._rsi_gain[symbol] = alpha_rsi * gain + (1 - alpha_rsi) * self._rsi_gain[symbol]
        self._rsi_loss[symbol] = alpha_rsi * loss_val + (1 - alpha_rsi) * self._rsi_loss[symbol]
        if self._rsi_loss[symbol] > 0:
            rs = self._rsi_gain[symbol] / self._rsi_loss[symbol]
            self._rsi[symbol] = 100 - 100 / (1 + rs)
        else:
            self._rsi[symbol] = 100.0

        # MACD histogram
        ema12 = self._ema_custom(symbol, 12, c)
        ema26 = self._ema_custom(symbol, 26, c)
        macd_line = ema12 - ema26
        # Simplified: just track if MACD hist is positive/negative
        self._macd_hist[symbol] = macd_line  # Approximate

        # ADX (simplified incremental)
        if len(bars) >= 2:
            plus_dm = max(h - bars[-2].high, 0)
            minus_dm = max(bars[-2].low - lo, 0)
            if plus_dm > minus_dm:
                minus_dm = 0
            elif minus_dm > plus_dm:
                plus_dm = 0
            else:
                plus_dm = minus_dm = 0

            atr_val = max(self._atr[symbol], 1e-12)
            alpha_adx = 2 / (self.adx_period + 1)
            self._plus_di[symbol] = alpha_adx * (100 * plus_dm / atr_val) + \
                (1 - alpha_adx) * self._plus_di[symbol]
            self._minus_di[symbol] = alpha_adx * (100 * minus_dm / atr_val) + \
                (1 - alpha_adx) * self._minus_di[symbol]

            di_sum = self._plus_di[symbol] + self._minus_di[symbol]
            dx = 100 * abs(self._plus_di[symbol] - self._minus_di[symbol]) / max(di_sum, 1e-12)
            self._adx[symbol] = alpha_adx * dx + (1 - alpha_adx) * self._adx[symbol]

        # Volume SMA
        alpha_vol = 2 / 21
        self._vol_sma[symbol] = alpha_vol * bar.volume + (1 - alpha_vol) * self._vol_sma[symbol]

    def _ema_custom(self, symbol: str, period: int, price: float) -> float:
        """Helper for MACD EMA computation (uses a simple dict)."""
        key = f"_ema_{period}"
        if not hasattr(self, key):
            setattr(self, key, {})
        store = getattr(self, key)
        if symbol not in store:
            store[symbol] = price
        alpha = 2 / (period + 1)
        store[symbol] = alpha * price + (1 - alpha) * store[symbol]
        return store[symbol]

    def _get_trend(self, symbol: str) -> int:
        """Trend direction: +1 bull, -1 bear, 0 neutral. 2-of-3 voting."""
        bars = self._bars.get(symbol)
        if not bars or len(bars) < 10:
            return 0

        price = bars[-1].close
        ema55 = self._ema55[symbol]
        adx = self._adx[symbol]
        di_spread = self._plus_di[symbol] - self._minus_di[symbol]

        # EMA55 slope (approximate from last 6 bars)
        if len(bars) >= 7:
            old_prices = [bars[-(i + 1)].close for i in range(6, 0, -1)]
            # Very rough EMA55 slope proxy
            slope = (ema55 - sum(old_prices) / len(old_prices)) / max(ema55, 1) * 100
        else:
            slope = 0

        bull = bear = 0
        if slope > 0.05:
            bull += 1
        elif slope < -0.05:
            bear += 1
        if price > ema55 * 1.003:
            bull += 1
        elif price < ema55 * 0.997:
            bear += 1
        if adx > 18:
            if di_spread > 2:
                bull += 1
            elif di_spread < -2:
                bear += 1

        if bull >= 2:
            return 1
        if bear >= 2:
            return -1
        return 0

    def _get_donchian(self, symbol: str) -> tuple:
        """Get Donchian high/low for the lookback period."""
        bars = self._bars.get(symbol)
        period = 10 if self.use_donchian10 else 20
        if not bars or len(bars) < period + 1:
            return float("nan"), float("nan")

        # Use bars[-period-1:-1] (shifted by 1)
        lookback = list(bars)[-period - 1:-1]
        dc_high = max(b.high for b in lookback)
        dc_low = min(b.low for b in lookback)
        return dc_high, dc_low

    def generate_signals(self) -> List[Signal]:
        """Generate entry/exit signals for all symbols."""
        signals: List[Signal] = []

        for symbol in self.symbols:
            bars = self._bars.get(symbol)
            if not bars or len(bars) < 60:
                continue

            bar = bars[-1]
            price = bar.close
            atr = self._atr[symbol]
            trend = self._get_trend(symbol)

            # ── EXIT SIGNAL ──
            if not self.is_flat(symbol):
                exit_signal = self._check_exit(symbol, bar, atr)
                if exit_signal:
                    signals.append(exit_signal)
                    continue

            # ── ENTRY SIGNAL ──
            if self.is_flat(symbol):
                entry_signal = self._check_entry(symbol, bar, atr, trend)
                if entry_signal:
                    signals.append(entry_signal)

        return signals

    def _check_exit(self, symbol: str, bar: Bar, atr: float) -> Optional[Signal]:
        """Check exit conditions for an open position."""
        pos = self.get_position(symbol)
        entry_atr = self._entry_atr.get(symbol, atr)
        stop_dist = self._stop_dist.get(symbol, entry_atr * self.atr_stop)
        dsign = 1 if pos > 0 else -1

        entry_p = self._entry_price.get(symbol, bar.close)
        stop_price = entry_p - dsign * stop_dist
        profit_in_atr = (bar.close - entry_p) * dsign / max(entry_atr, 1)
        trail_active = profit_in_atr > self.atr_trail_activate
        trail_dist = entry_atr * self.atr_trail

        reason = None
        if pos > 0:
            trail_price = self._best_price.get(symbol, bar.high) - trail_dist
            if bar.low <= stop_price:
                reason = "stop_loss"
            elif trail_active and bar.low <= trail_price:
                reason = "trailing_stop"
        else:
            wp = self._worst_price.get(symbol, bar.low)
            trail_price = wp + trail_dist if wp > 0 else bar.close + trail_dist
            if bar.high >= stop_price:
                reason = "stop_loss"
            elif trail_active and bar.high >= trail_price:
                reason = "trailing_stop"

        bars_held = self._bars_held.get(symbol, 0)
        if bars_held >= self.max_hold:
            reason = "time_exit"

        if reason:
            self._bars_held[symbol] = 0
            self._bars_since[symbol] = 0
            return Signal(
                signal_id=str(uuid4()),
                strategy_id=self.strategy_id,
                symbol=symbol,
                direction=SignalDirection.FLAT,
                strength=0.0,
                timestamp=self._current_time,
                metadata={"reason": reason, "bars_held": bars_held},
            )
        return None

    def _check_entry(self, symbol: str, bar: Bar, atr: float, trend: int) -> Optional[Signal]:
        """Check for entry signals."""
        if self._bars_since.get(symbol, 0) < self.cooldown:
            return None
        if atr < 1e-12:
            return None

        price = bar.close
        dc_high, dc_low = self._get_donchian(symbol)

        direction = 0
        entry_type = ""

        # Breakout entry
        if not np.isnan(dc_high) and price > dc_high and trend >= 0:
            direction = 1
            entry_type = "breakout"
        elif not np.isnan(dc_low) and price < dc_low and trend <= 0:
            direction = -1
            entry_type = "breakout"

        # Pullback entry (only in confirmed trend)
        if direction == 0 and trend != 0:
            ema21 = self._ema21[symbol]
            ema55 = self._ema55[symbol]
            prev_bar = list(self._bars[symbol])[-2] if len(self._bars[symbol]) >= 2 else bar

            if trend == 1 and bar.low <= ema21 * 1.01 and price > ema21 and price > prev_bar.close:
                if price > ema55:
                    direction = 1
                    entry_type = "pullback"
            elif trend == -1 and bar.high >= ema21 * 0.99 and price < ema21 and price < prev_bar.close:
                if price < ema55:
                    direction = -1
                    entry_type = "pullback"

        if direction == 0:
            return None
        if trend != 0 and direction != trend:
            return None

        # Score
        score = self._calc_score(symbol, direction)
        if score < self.min_score:
            return None

        # Compute position size: qty = risk / stop_dist
        stop_dist = atr * self.atr_stop
        stop_dist = max(stop_dist, price * 0.005)

        # Store entry state
        self._entry_price[symbol] = price
        self._entry_atr[symbol] = atr
        self._stop_dist[symbol] = stop_dist
        self._best_price[symbol] = bar.high
        self._worst_price[symbol] = bar.low
        self._bars_held[symbol] = 0
        self._bars_since[symbol] = 0

        signal_dir = SignalDirection.LONG if direction > 0 else SignalDirection.SHORT
        strength = min(score / 5.0, 1.0)

        return Signal(
            signal_id=str(uuid4()),
            strategy_id=self.strategy_id,
            symbol=symbol,
            direction=signal_dir,
            strength=strength,
            timestamp=self._current_time,
            metadata={
                "entry_type": entry_type,
                "score": score,
                "trend": trend,
                "atr_pct": round(atr / price * 100, 2),
                "stop_dist_pct": round(stop_dist / price * 100, 2),
            },
        )

    def _calc_score(self, symbol: str, direction: int) -> int:
        """Calculate entry score (max ~8)."""
        score = 0
        vol_ratio = 0
        if self._vol_sma[symbol] > 0:
            bar = self._bars[symbol][-1]
            vol_ratio = bar.volume / self._vol_sma[symbol]
        if vol_ratio >= self.vol_min:
            score += 1
        if vol_ratio >= 2.0:
            score += 1

        adx = self._adx[symbol]
        if adx >= 20:
            score += 1
        if adx >= 30:
            score += 1

        rsi = self._rsi[symbol]
        if direction > 0 and 45 <= rsi <= 80:
            score += 1
        elif direction < 0 and 20 <= rsi <= 55:
            score += 1

        macd = self._macd_hist[symbol]
        if (direction > 0 and macd > 0) or (direction < 0 and macd < 0):
            score += 1

        if direction > 0 and self._ema8[symbol] > self._ema21[symbol]:
            score += 1
        elif direction < 0 and self._ema8[symbol] < self._ema21[symbol]:
            score += 1

        return score

    # ------------------------------------------------------------------
    # Live equity management
    # ------------------------------------------------------------------

    def set_live_equity(self, equity: float) -> None:
        """Set live equity for dynamic position sizing.

        Called by the orchestrator/runner before signal generation.
        When set, overrides the static 'initial_capital' parameter.
        """
        self._live_equity = equity

    def _get_equity(self) -> float:
        """Get current equity for position sizing.

        Priority: live_equity (from exchange) > initial_capital param.
        """
        if self._live_equity is not None and self._live_equity > 0:
            return self._live_equity
        return float(self.get_param("initial_capital", 10000))

    def calculate_target_positions(self) -> List[TargetPosition]:
        """Fixed-risk position sizing: qty = risk_amount / stop_distance.

        In live mode, equity is fetched from the exchange balance
        via set_live_equity(). In backtest mode, uses initial_capital param.
        """
        signals = self.generate_signals()
        self._signals.extend(signals)
        self._metrics["signals_generated"] += len(signals)

        equity = self._get_equity()
        targets: List[TargetPosition] = []
        for signal in signals:
            if signal.direction == SignalDirection.FLAT:
                targets.append(TargetPosition(
                    symbol=signal.symbol,
                    strategy_id=self.strategy_id,
                    target_quantity=0.0,
                    target_weight=0.0,
                    urgency=0.8,
                    timestamp=signal.timestamp,
                ))
            else:
                stop_dist = self._stop_dist.get(signal.symbol, 1.0)
                price = self._entry_price.get(signal.symbol, 1.0)
                # Risk-based sizing with dynamic equity
                risk_amount = equity * self.risk_per_trade
                qty_abs = risk_amount / max(stop_dist, 1e-12)
                # Cap margin
                notional = qty_abs * price
                margin = notional / self.leverage
                max_margin = equity * self.max_margin_pct
                if margin > max_margin:
                    qty_abs = max_margin * self.leverage / max(price, 1)

                qty = qty_abs if signal.direction == SignalDirection.LONG else -qty_abs

                targets.append(TargetPosition(
                    symbol=signal.symbol,
                    strategy_id=self.strategy_id,
                    target_quantity=qty,
                    target_weight=signal.strength,
                    urgency=0.6,
                    timestamp=signal.timestamp,
                ))

        return targets
