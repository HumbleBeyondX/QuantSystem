"""Mid-frequency crypto microstructure trading strategy.

Combines orderbook and trade flow factors on 5-15 minute bars to detect
short-term directional pressure. Designed for retail traders:

- NOT HFT: operates on bar intervals (5min default), not tick-by-tick
- Cost-aware: profitability gate rejects trades where expected profit < 2x costs
- Liquidity-adaptive: position sizing respects orderbook depth
- Robust risk management: stop loss, cooldown, daily trade limits

Factor composition:
    OBI (30%)       - Order Book Imbalance, multi-level time-averaged
    TFI (25%)       - Trade Flow Imbalance, buy vs sell aggressor volume
    Microprice (20%) - Microprice deviation from mid
    Whale (10%)     - Large trade directional imbalance
    Spread (10%)    - Spread z-score (tight spread = favorable entry)
    VPIN (5%)       - Volume-Synchronized Informed Trading (contrarian/risk)

Typical holding period: 15min - 2h
Target pairs: BTC/USDT, ETH/USDT (most liquid = lowest cost)
"""

from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, List, Optional, Tuple
from uuid import uuid4

import numpy as np

from core.enums import SignalDirection
from core.models import Bar, OrderBookSnapshot, Signal, TargetPosition
from data.features.microstructure_midfreq import (
    compute_composite_score,
    compute_microprice_deviation,
    compute_obi_midfreq,
    compute_spread_zscore,
    compute_tfi_midfreq,
    compute_vpin_bars,
    compute_whale_signal,
)
from execution.models.cost_model import CostModel
from strategies.base import StrategyBase, StrategyConfig
from strategies.registry import register_strategy


@register_strategy("crypto_microstructure")
class MicrostructureStrategy(StrategyBase):
    """Mid-frequency crypto strategy based on microstructure factors.

    Accumulates orderbook snapshots and trades between bars, then computes
    a weighted composite of 6 microstructure factors to generate directional
    signals. All trades must pass a cost-profitability gate before execution.
    """

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

        # ── Timeframe ──
        self.bar_timeframe = self.get_param("bar_timeframe", "5m")
        self.min_warmup_bars = self.get_param("min_warmup_bars", 20)

        # ── Factor weights ──
        self.factor_weights = {
            "obi": self.get_param("weight_obi", 0.30),
            "tfi": self.get_param("weight_tfi", 0.25),
            "microprice_dev": self.get_param("weight_microprice_dev", 0.20),
            "whale": self.get_param("weight_whale", 0.10),
            "spread_zscore": self.get_param("weight_spread_zscore", 0.10),
            "vpin": self.get_param("weight_vpin", 0.05),
        }

        # ── Orderbook parameters ──
        self.depth_levels = self.get_param("depth_levels", 10)
        self.obi_decay = self.get_param("obi_decay", 0.94)

        # ── Trade flow parameters ──
        self.tfi_window_seconds = self.get_param("tfi_window_seconds", 300)
        self.whale_multiplier = self.get_param("whale_multiplier", 10.0)

        # ── VPIN ──
        self.vpin_n_buckets = self.get_param("vpin_n_buckets", 50)

        # ── Spread ──
        self.spread_lookback = self.get_param("spread_lookback", 50)
        self.max_spread_bps = self.get_param("max_spread_bps", 15.0)

        # ── Signal thresholds ──
        self.entry_threshold = self.get_param("entry_threshold", 0.35)
        self.exit_threshold = self.get_param("exit_threshold", 0.15)
        self.composite_scale = self.get_param("composite_scale", 50.0)

        # ── Cost model ──
        self._cost_model = CostModel(
            maker_fee=self.get_param("maker_fee_rate", 0.0002),
            taker_fee=self.get_param("taker_fee_rate", 0.0004),
            base_slippage_bps=self.get_param("base_slippage_bps", 1.0),
            depth_impact_factor=self.get_param("depth_impact_factor", 0.05),
            min_profit_to_cost_ratio=self.get_param("min_profit_to_cost_ratio", 2.0),
        )

        # ── Position sizing ──
        self.max_position_usd = self.get_param("max_position_usd", 5000.0)
        self.position_scale_by_strength = self.get_param("position_scale_by_strength", True)

        # ── Risk management ──
        self.stop_loss_bps = self.get_param("stop_loss_bps", 30.0)
        self.max_daily_trades = self.get_param("max_daily_trades", 50)
        self.cooldown_base_seconds = self.get_param("cooldown_base_seconds", 60)
        self.max_consecutive_losses = self.get_param("max_consecutive_losses", 3)
        self.extended_cooldown_minutes = self.get_param("extended_cooldown_minutes", 30)

        # ── Internal state ──
        # Buffers: accumulate data between bars
        self._ob_buffer: Dict[str, List[Dict[str, Any]]] = {}
        self._trade_buffer: Dict[str, List[Dict[str, Any]]] = {}

        # History: rolling windows of computed values
        self._bar_history: Dict[str, Deque[Dict[str, float]]] = {}
        self._factor_history: Dict[str, Deque[Dict[str, float]]] = {}
        self._spread_history: Dict[str, Deque[float]] = {}

        # Trade state
        self._trade_median_amount: Dict[str, float] = {}
        self._entry_price: Dict[str, float] = {}
        self._entry_time: Dict[str, datetime] = {}
        self._daily_trade_count: int = 0
        self._last_trade_day: Optional[str] = None

        # Cooldown tracking
        self._cooldown_until: Dict[str, datetime] = {}
        self._consecutive_losses: Dict[str, int] = {}
        self._last_pnl: Dict[str, float] = {}

        # Latest composite scores for logging
        self._latest_scores: Dict[str, float] = {}
        self._latest_factors: Dict[str, Dict[str, float]] = {}

    # ================================================================
    # Data Handlers
    # ================================================================

    def on_orderbook(self, symbol: str, orderbook: OrderBookSnapshot) -> None:
        """Accumulate orderbook snapshots between bars.

        Each snapshot is converted to a lightweight dict for factor computation.
        We keep the buffer bounded to prevent memory issues.
        """
        if symbol not in self._ob_buffer:
            self._ob_buffer[symbol] = []
            self._spread_history[symbol] = deque(maxlen=self.spread_lookback * 3)

        # Convert to lightweight dict
        bids = [(l.price, l.quantity) for l in orderbook.bids[:self.depth_levels]]
        asks = [(l.price, l.quantity) for l in orderbook.asks[:self.depth_levels]]

        snap = {
            "bids": bids,
            "asks": asks,
            "best_bid": orderbook.best_bid,
            "best_ask": orderbook.best_ask,
            "bid_qty": orderbook.bids[0].quantity if orderbook.bids else 0,
            "ask_qty": orderbook.asks[0].quantity if orderbook.asks else 0,
            "timestamp": orderbook.timestamp,
        }
        self._ob_buffer[symbol].append(snap)

        # Compute spread for running history
        if orderbook.best_bid > 0 and orderbook.best_ask > 0:
            mid = (orderbook.best_bid + orderbook.best_ask) / 2
            spread_bps = (orderbook.best_ask - orderbook.best_bid) / mid * 10_000
            self._spread_history[symbol].append(spread_bps)

        # Bound buffer size (keep last 200 snapshots between bars)
        if len(self._ob_buffer[symbol]) > 200:
            self._ob_buffer[symbol] = self._ob_buffer[symbol][-200:]

    def on_trade(self, symbol: str, trade: Dict[str, Any]) -> None:
        """Accumulate individual trades between bars.

        Expected trade dict: {"side": "buy"/"sell", "amount": float, "price": float}
        """
        if symbol not in self._trade_buffer:
            self._trade_buffer[symbol] = []

        self._trade_buffer[symbol].append(trade)

        # Update running median trade size
        amount = float(trade.get("amount", 0))
        if amount > 0:
            current_median = self._trade_median_amount.get(symbol, amount)
            # Exponential moving median approximation
            self._trade_median_amount[symbol] = current_median * 0.99 + amount * 0.01

        # Bound buffer
        if len(self._trade_buffer[symbol]) > 5000:
            self._trade_buffer[symbol] = self._trade_buffer[symbol][-5000:]

    def on_bar(self, symbol: str, bar: Bar) -> None:
        """Process a completed bar: compute factors from accumulated data.

        This is the main processing cadence. When a new bar arrives:
        1. Compute all 6 factors from accumulated orderbook & trade buffers
        2. Store factor values in history
        3. Clear buffers for next bar period
        """
        super().on_bar(symbol, bar)

        # Initialize history structures
        if symbol not in self._bar_history:
            self._bar_history[symbol] = deque(maxlen=200)
            self._factor_history[symbol] = deque(maxlen=200)

        # Store bar data
        bar_dict = {
            "open": bar.open, "high": bar.high, "low": bar.low,
            "close": bar.close, "volume": bar.volume,
            "timestamp": bar.timestamp,
        }
        self._bar_history[symbol].append(bar_dict)

        # Compute factors from accumulated buffers
        factors = self._compute_factors(symbol)
        self._factor_history[symbol].append(factors)
        self._latest_factors[symbol] = factors

        # Clear buffers for next bar
        self._ob_buffer[symbol] = []
        self._trade_buffer[symbol] = []

        # Reset daily trade counter if new day
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if self._last_trade_day != today:
            self._daily_trade_count = 0
            self._last_trade_day = today

    # ================================================================
    # Factor Computation
    # ================================================================

    def _compute_factors(self, symbol: str) -> Dict[str, float]:
        """Compute all 6 microstructure factors for the current bar."""
        ob_snaps = self._ob_buffer.get(symbol, [])
        trades = self._trade_buffer.get(symbol, [])
        bar_hist = list(self._bar_history.get(symbol, []))

        # Factor 1: OBI
        obi = compute_obi_midfreq(ob_snaps, levels=self.depth_levels, decay=self.obi_decay)

        # Factor 2: TFI
        tfi = compute_tfi_midfreq(trades, window_seconds=self.tfi_window_seconds)

        # Factor 3: Spread Z-Score
        spread_hist = self._spread_history.get(symbol, deque())
        current_spread = float(np.mean(list(spread_hist)[-5:])) if spread_hist else 0.0
        spread_zscore = compute_spread_zscore(
            current_spread, spread_hist, lookback=self.spread_lookback
        )

        # Factor 4: Microprice Deviation
        microprice_dev = compute_microprice_deviation(ob_snaps)

        # Factor 5: Whale Signal
        median_amt = self._trade_median_amount.get(symbol, 1.0)
        whale = compute_whale_signal(trades, median_amt, self.whale_multiplier)
        whale_imbalance = whale["whale_imbalance"]

        # Factor 6: VPIN
        vpin = compute_vpin_bars(bar_hist, n_buckets=self.vpin_n_buckets)

        # Composite
        composite = compute_composite_score(
            obi=obi,
            tfi=tfi,
            spread_zscore=spread_zscore,
            microprice_dev=microprice_dev,
            whale_imbalance=whale_imbalance,
            vpin=vpin,
            weights=self.factor_weights,
        )
        self._latest_scores[symbol] = composite

        return {
            "obi": obi,
            "tfi": tfi,
            "spread_zscore": spread_zscore,
            "microprice_dev": microprice_dev,
            "whale_imbalance": whale_imbalance,
            "vpin": vpin,
            "composite": composite,
            "current_spread_bps": current_spread,
        }

    # ================================================================
    # Signal Generation
    # ================================================================

    def generate_signals(self) -> List[Signal]:
        """Generate trading signals from computed microstructure factors.

        Logic:
        1. Check warmup (need min_warmup_bars of history)
        2. Check daily trade limit
        3. For each symbol:
            a. Check cooldown
            b. Check spread (too wide = skip)
            c. Get composite score
            d. If no position: check entry threshold + cost gate
            e. If in position: check exit conditions
        """
        signals: List[Signal] = []
        now = datetime.utcnow()

        # Daily trade limit
        if self._daily_trade_count >= self.max_daily_trades:
            return signals

        for symbol in self.symbols:
            factor_hist = self._factor_history.get(symbol)
            if not factor_hist or len(factor_hist) < self.min_warmup_bars:
                continue

            latest = factor_hist[-1]
            composite = latest["composite"]
            spread_bps = latest.get("current_spread_bps", 0)

            # ── Spread filter ──
            if spread_bps > self.max_spread_bps:
                continue

            # ── Cooldown check ──
            cooldown_end = self._cooldown_until.get(symbol)
            if cooldown_end and now < cooldown_end:
                continue

            # ── Get current price ──
            last_bar = self._last_bars.get(symbol)
            if not last_bar:
                continue
            price = last_bar.close

            # ── Estimate orderbook depth ──
            ob_depth = self._estimate_ob_depth(symbol)

            # ── Position management ──
            current_pos = self.get_position(symbol)

            if abs(current_pos) < 1e-10:
                # No position: evaluate entry
                signal = self._evaluate_entry(
                    symbol, composite, price, ob_depth, now
                )
                if signal:
                    signals.append(signal)
            else:
                # In position: evaluate exit
                signal = self._evaluate_exit(
                    symbol, composite, price, current_pos, now
                )
                if signal:
                    signals.append(signal)

        return signals

    def _evaluate_entry(
        self,
        symbol: str,
        composite: float,
        price: float,
        ob_depth: float,
        now: datetime,
    ) -> Optional[Signal]:
        """Evaluate whether to enter a new position.

        Must pass:
        1. Signal threshold: abs(composite) > entry_threshold
        2. Cost gate: expected profit > min_ratio * round_trip_cost
        """
        if abs(composite) < self.entry_threshold:
            return None

        # Expected move in bps
        expected_move_bps = abs(composite) * self.composite_scale

        # Position size
        qty = self._cost_model.optimal_quantity(
            signal_strength=composite,
            max_position_usd=self.max_position_usd,
            price=price,
            orderbook_depth=ob_depth,
        )

        if qty < 1e-8:
            return None

        # Cost gate
        if not self._cost_model.is_trade_profitable(
            expected_move_bps=expected_move_bps,
            quantity=qty,
            price=price,
            orderbook_depth=ob_depth,
        ):
            return None

        # Determine direction
        direction = SignalDirection.LONG if composite > 0 else SignalDirection.SHORT

        cost_breakdown = self._cost_model.estimate_round_trip_cost(price, qty, ob_depth)

        return Signal(
            signal_id=uuid4(),
            strategy_id=self.strategy_id,
            symbol=symbol,
            direction=direction,
            strength=abs(composite),
            timestamp=now,
            target_price=price,
            target_quantity=qty,
            metadata={
                "composite": round(composite, 4),
                "expected_move_bps": round(expected_move_bps, 2),
                "cost_bps": round(cost_breakdown.total_bps, 2),
                "cost_usd": round(cost_breakdown.total_usd, 4),
                "factors": self._latest_factors.get(symbol, {}),
                "reason": "entry",
            },
        )

    def _evaluate_exit(
        self,
        symbol: str,
        composite: float,
        price: float,
        current_pos: float,
        now: datetime,
    ) -> Optional[Signal]:
        """Evaluate whether to exit current position.

        Exit conditions:
        1. Signal reversal: composite crosses zero against position
        2. Signal decay: abs(composite) drops below exit_threshold
        3. Stop loss: price moved against position by stop_loss_bps
        """
        entry_price = self._entry_price.get(symbol)
        if entry_price is None or entry_price <= 0:
            entry_price = price  # Fallback

        is_long = current_pos > 0

        # Price change since entry in bps
        price_change_bps = (price - entry_price) / entry_price * 10_000
        if not is_long:
            price_change_bps = -price_change_bps  # Invert for shorts

        exit_reason = None

        # Condition 1: Signal reversal
        if is_long and composite < -self.exit_threshold:
            exit_reason = "signal_reversal"
        elif not is_long and composite > self.exit_threshold:
            exit_reason = "signal_reversal"

        # Condition 2: Signal decay (composite faded to near-zero)
        if exit_reason is None and abs(composite) < self.exit_threshold * 0.5:
            exit_reason = "signal_decay"

        # Condition 3: Stop loss
        if exit_reason is None and price_change_bps < -self.stop_loss_bps:
            exit_reason = "stop_loss"

        if exit_reason is None:
            return None

        # Track P&L for cooldown logic
        self._record_exit(symbol, price_change_bps, now)

        return Signal(
            signal_id=uuid4(),
            strategy_id=self.strategy_id,
            symbol=symbol,
            direction=SignalDirection.FLAT,
            strength=0.8,
            timestamp=now,
            target_price=price,
            target_quantity=0.0,
            metadata={
                "composite": round(composite, 4),
                "pnl_bps": round(price_change_bps, 2),
                "reason": exit_reason,
                "entry_price": entry_price,
                "exit_price": price,
            },
        )

    # ================================================================
    # Position Tracking & Risk
    # ================================================================

    def on_fill(self, fill: Any) -> None:
        """Track fills for entry price and trade counting."""
        super().on_fill(fill)

        symbol = fill.symbol
        new_pos = self.get_position(symbol)

        if abs(new_pos) > 1e-10 and symbol not in self._entry_price:
            # New position opened
            self._entry_price[symbol] = fill.price
            self._entry_time[symbol] = datetime.utcnow()
            self._daily_trade_count += 1
        elif abs(new_pos) < 1e-10:
            # Position closed
            self._entry_price.pop(symbol, None)
            self._entry_time.pop(symbol, None)

    def _record_exit(self, symbol: str, pnl_bps: float, now: datetime) -> None:
        """Record exit P&L and manage cooldown logic."""
        self._last_pnl[symbol] = pnl_bps

        if pnl_bps < 0:
            # Loss — increment consecutive losses, apply cooldown
            losses = self._consecutive_losses.get(symbol, 0) + 1
            self._consecutive_losses[symbol] = losses

            if losses >= self.max_consecutive_losses:
                # Extended cooldown
                cooldown = timedelta(minutes=self.extended_cooldown_minutes)
                self._consecutive_losses[symbol] = 0  # Reset after extended cooldown
            else:
                # Normal cooldown (escalating)
                cooldown = timedelta(seconds=self.cooldown_base_seconds * (1 + losses))

            self._cooldown_until[symbol] = now + cooldown
        else:
            # Win — reset consecutive losses
            self._consecutive_losses[symbol] = 0

    def _estimate_ob_depth(self, symbol: str) -> float:
        """Estimate current orderbook depth from recent snapshots.

        Returns total bid+ask volume at top N levels.
        """
        ob_snaps = self._ob_buffer.get(symbol, [])
        if not ob_snaps:
            return 1.0  # Conservative default

        # Use latest snapshot
        latest = ob_snaps[-1]
        bids = latest.get("bids", [])
        asks = latest.get("asks", [])

        bid_depth = sum(qty for _, qty in bids[:self.depth_levels])
        ask_depth = sum(qty for _, qty in asks[:self.depth_levels])

        return (bid_depth + ask_depth) / 2.0 if (bid_depth + ask_depth) > 0 else 1.0

    # ================================================================
    # Target Positions (cost-aware sizing)
    # ================================================================

    def calculate_target_positions(self) -> List[TargetPosition]:
        """Override base class to use cost-aware position sizing."""
        signals = self.generate_signals()
        self._signals.extend(signals)
        self._metrics["signals_generated"] += len(signals)

        targets: List[TargetPosition] = []
        for signal in signals:
            if signal.direction == SignalDirection.FLAT:
                target_qty = 0.0
            elif signal.target_quantity and signal.target_quantity > 0:
                # Use quantity from signal (already optimized by cost model)
                sign = 1.0 if signal.direction == SignalDirection.LONG else -1.0
                target_qty = sign * signal.target_quantity
            else:
                target_qty = signal.strength

            targets.append(
                TargetPosition(
                    symbol=signal.symbol,
                    strategy_id=self.strategy_id,
                    target_quantity=target_qty,
                    target_weight=signal.strength,
                    urgency=0.3,  # Patient — prefer limit orders for maker fees
                    timestamp=signal.timestamp,
                )
            )

        return targets

    # ================================================================
    # Diagnostics
    # ================================================================

    @property
    def diagnostics(self) -> Dict[str, Any]:
        """Return current strategy state for dashboard/logging."""
        return {
            "latest_scores": {k: round(v, 4) for k, v in self._latest_scores.items()},
            "latest_factors": self._latest_factors,
            "daily_trades": self._daily_trade_count,
            "cooldowns": {
                k: v.isoformat() for k, v in self._cooldown_until.items()
                if v > datetime.utcnow()
            },
            "consecutive_losses": dict(self._consecutive_losses),
            "positions": {k: v for k, v in self._positions.items() if abs(v) > 1e-10},
            "entry_prices": dict(self._entry_price),
            "factor_history_len": {
                k: len(v) for k, v in self._factor_history.items()
            },
        }
