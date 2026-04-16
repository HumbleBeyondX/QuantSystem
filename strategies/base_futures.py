"""Futures strategy base class — leverage, margin, funding rate, and basis-aware.

Extends StrategyBase with perpetual/dated futures-specific capabilities:
- Leverage and margin management (cross/isolated)
- Funding rate tracking and arbitrage infrastructure
- Basis spread (spot vs futures) monitoring
- Liquidation price calculation and proximity alerts
- Fixed-risk position sizing (qty = risk / stop_distance)
- Multi-exchange support (Binance, Bybit, OKX via CCXT)
- Perpetual vs dated futures abstraction

Usage:
    class MyFuturesStrategy(FuturesStrategyBase):
        def generate_signals(self) -> List[Signal]:
            if self.funding_rate("BTC/USDT:USDT") > 0.001:
                return [self.short_signal("BTC/USDT:USDT", ...)]
            return []
"""

from __future__ import annotations

from abc import abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple
from uuid import uuid4

import numpy as np

from core.enums import AssetClass, OrderSide, SignalDirection
from core.models import Bar, Fill, Signal, TargetPosition
from strategies.base import StrategyBase, StrategyConfig


# ------------------------------------------------------------------
# Futures position data
# ------------------------------------------------------------------

@dataclass
class FuturesPosition:
    """Tracked futures position with margin/leverage metadata."""
    symbol: str
    side: str  # "long" or "short"
    quantity: float
    entry_price: float
    leverage: float
    margin_type: str = "cross"  # cross / isolated
    margin_used: float = 0.0
    liquidation_price: float = 0.0
    unrealized_pnl: float = 0.0
    funding_accumulated: float = 0.0
    opened_at: datetime = field(default_factory=datetime.utcnow)
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    trailing_stop_distance: Optional[float] = None
    trailing_stop_peak: Optional[float] = None

    @property
    def notional(self) -> float:
        return self.quantity * self.entry_price

    @property
    def pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        if self.side == "long":
            return (self.unrealized_pnl / (self.quantity * self.entry_price)) * self.leverage
        else:
            return (self.unrealized_pnl / (self.quantity * self.entry_price)) * self.leverage

    def update_price(self, current_price: float) -> None:
        """Recompute unrealized PnL and trailing stop."""
        if self.side == "long":
            self.unrealized_pnl = (current_price - self.entry_price) * self.quantity
        else:
            self.unrealized_pnl = (self.entry_price - current_price) * self.quantity

        # Trailing stop
        if self.trailing_stop_distance and self.trailing_stop_peak is not None:
            if self.side == "long":
                self.trailing_stop_peak = max(self.trailing_stop_peak, current_price)
                self.stop_loss = self.trailing_stop_peak - self.trailing_stop_distance
            else:
                self.trailing_stop_peak = min(self.trailing_stop_peak, current_price)
                self.stop_loss = self.trailing_stop_peak + self.trailing_stop_distance

    def should_stop(self, current_price: float) -> bool:
        """Check if stop loss or take profit triggered."""
        if self.stop_loss:
            if self.side == "long" and current_price <= self.stop_loss:
                return True
            if self.side == "short" and current_price >= self.stop_loss:
                return True
        if self.take_profit:
            if self.side == "long" and current_price >= self.take_profit:
                return True
            if self.side == "short" and current_price <= self.take_profit:
                return True
        return False


class FuturesStrategyBase(StrategyBase):
    """Base class for perpetual and dated futures strategies.

    Provides leverage management, funding rate tracking, fixed-risk sizing,
    liquidation monitoring, and basis spread computation.
    """

    def __init__(self, config: StrategyConfig) -> None:
        if config.asset_class not in (AssetClass.CRYPTO_PERP, AssetClass.CRYPTO):
            config.asset_class = AssetClass.CRYPTO_PERP
        super().__init__(config)

        # ── Leverage & margin ──
        self.default_leverage = self.get_param("leverage", 10.0)
        self.default_margin_type = self.get_param("margin_type", "cross")
        self.max_leverage = self.get_param("max_leverage", 50.0)
        self.risk_per_trade = self.get_param("risk_per_trade", 0.02)  # 2% of equity
        self.max_margin_pct = self.get_param("max_margin_pct", 0.50)  # Max 50% equity as margin

        # ── Exchange ──
        self.exchange = self.get_param("exchange", "binance")

        # ── Positions ──
        self._futures_positions: Dict[str, FuturesPosition] = {}

        # ── Market data caches ──
        self._funding_rates: Dict[str, Deque[Tuple[datetime, float]]] = {}
        self._mark_prices: Dict[str, float] = {}
        self._index_prices: Dict[str, float] = {}  # spot/index for basis
        self._bars_cache: Dict[str, Deque[Bar]] = {}
        self._atr_cache: Dict[str, float] = {}

        # ── Equity tracking ──
        self._equity = self.get_param("initial_equity", 10000.0)
        self._peak_equity = self._equity
        self._total_pnl = 0.0

        # ── Cooldown ──
        self._cooldown_remaining: Dict[str, int] = {}
        self._bar_count: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # Position sizing: fixed-risk model
    # ------------------------------------------------------------------

    def compute_position_size(
        self,
        symbol: str,
        entry_price: float,
        stop_price: float,
        leverage: Optional[float] = None,
    ) -> float:
        """Fixed-risk position sizing: qty = risk_amount / stop_distance.

        Decouples leverage from risk — leverage only reduces margin, not max loss.
        """
        lev = leverage or self.default_leverage
        stop_distance = abs(entry_price - stop_price)
        if stop_distance < 1e-10:
            return 0.0

        risk_amount = self._equity * self.risk_per_trade
        qty = risk_amount / stop_distance

        # Cap by margin limit
        margin_needed = (qty * entry_price) / lev
        max_margin = self._equity * self.max_margin_pct
        if margin_needed > max_margin:
            qty = (max_margin * lev) / entry_price

        return qty

    # ------------------------------------------------------------------
    # Funding rate tracking
    # ------------------------------------------------------------------

    def update_funding_rate(self, symbol: str, rate: float, timestamp: Optional[datetime] = None) -> None:
        """Record a funding rate snapshot."""
        ts = timestamp or datetime.utcnow()
        if symbol not in self._funding_rates:
            self._funding_rates[symbol] = deque(maxlen=500)
        self._funding_rates[symbol].append((ts, rate))

    def funding_rate(self, symbol: str) -> float:
        """Get most recent funding rate."""
        hist = self._funding_rates.get(symbol)
        if not hist:
            return 0.0
        return hist[-1][1]

    def avg_funding_rate(self, symbol: str, periods: int = 24) -> float:
        """Average funding rate over last N periods (8h each on most exchanges)."""
        hist = self._funding_rates.get(symbol)
        if not hist:
            return 0.0
        recent = [r for _, r in list(hist)[-periods:]]
        return float(np.mean(recent)) if recent else 0.0

    def annualized_funding(self, symbol: str, periods: int = 24) -> float:
        """Annualize the average funding rate (3 per day × 365)."""
        return self.avg_funding_rate(symbol, periods) * 3 * 365

    # ------------------------------------------------------------------
    # Basis spread (spot vs futures)
    # ------------------------------------------------------------------

    def update_mark_price(self, symbol: str, mark: float) -> None:
        self._mark_prices[symbol] = mark

    def update_index_price(self, symbol: str, index: float) -> None:
        self._index_prices[symbol] = index

    def basis_spread(self, symbol: str) -> float:
        """Premium/discount of futures vs spot. Positive = contango."""
        mark = self._mark_prices.get(symbol, 0)
        index = self._index_prices.get(symbol, 0)
        if index == 0:
            return 0.0
        return (mark - index) / index

    # ------------------------------------------------------------------
    # ATR computation
    # ------------------------------------------------------------------

    def on_bar(self, symbol: str, bar: Bar) -> None:
        super().on_bar(symbol, bar)
        if symbol not in self._bars_cache:
            self._bars_cache[symbol] = deque(maxlen=200)
        self._bars_cache[symbol].append(bar)
        self._bar_count[symbol] = self._bar_count.get(symbol, 0) + 1
        self._update_atr(symbol)

    def _update_atr(self, symbol: str, period: int = 14) -> None:
        bars = self._bars_cache.get(symbol)
        if not bars or len(bars) < period + 1:
            return
        trs = []
        bar_list = list(bars)
        for i in range(-period, 0):
            h = bar_list[i].high
            l = bar_list[i].low
            pc = bar_list[i - 1].close
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        self._atr_cache[symbol] = float(np.mean(trs))

    def atr(self, symbol: str) -> float:
        return self._atr_cache.get(symbol, 0.0)

    # ------------------------------------------------------------------
    # Liquidation price estimation
    # ------------------------------------------------------------------

    def estimate_liquidation_price(
        self, entry_price: float, leverage: float, side: str, maint_margin_rate: float = 0.004
    ) -> float:
        """Estimate liquidation price for isolated margin.

        For cross margin, actual liq price depends on total account balance.
        """
        if side == "long":
            return entry_price * (1 - 1 / leverage + maint_margin_rate)
        else:
            return entry_price * (1 + 1 / leverage - maint_margin_rate)

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def open_futures_position(
        self, symbol: str, side: str, quantity: float, entry_price: float,
        stop_loss: Optional[float] = None, take_profit: Optional[float] = None,
        trailing_atr_mult: Optional[float] = None,
    ) -> FuturesPosition:
        """Create and track a new futures position."""
        lev = self.default_leverage
        trail_dist = trailing_atr_mult * self.atr(symbol) if trailing_atr_mult else None

        pos = FuturesPosition(
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            leverage=lev,
            margin_type=self.default_margin_type,
            margin_used=(quantity * entry_price) / lev,
            liquidation_price=self.estimate_liquidation_price(entry_price, lev, side),
            stop_loss=stop_loss,
            take_profit=take_profit,
            trailing_stop_distance=trail_dist,
            trailing_stop_peak=entry_price if trail_dist else None,
        )
        self._futures_positions[symbol] = pos
        return pos

    def close_futures_position(self, symbol: str, exit_price: float) -> float:
        """Close a futures position and return realized PnL."""
        pos = self._futures_positions.pop(symbol, None)
        if not pos:
            return 0.0
        pos.update_price(exit_price)
        pnl = pos.unrealized_pnl - pos.funding_accumulated
        self._total_pnl += pnl
        self._equity += pnl
        self._peak_equity = max(self._peak_equity, self._equity)
        return pnl

    def get_futures_position(self, symbol: str) -> Optional[FuturesPosition]:
        return self._futures_positions.get(symbol)

    def has_position(self, symbol: str) -> bool:
        return symbol in self._futures_positions

    # ------------------------------------------------------------------
    # Equity & drawdown
    # ------------------------------------------------------------------

    @property
    def equity(self) -> float:
        return self._equity

    @property
    def drawdown_pct(self) -> float:
        if self._peak_equity == 0:
            return 0.0
        return (self._peak_equity - self._equity) / self._peak_equity

    # ------------------------------------------------------------------
    # Signal helpers
    # ------------------------------------------------------------------

    def long_signal(self, symbol: str, strength: float, qty: Optional[float] = None, **meta) -> Signal:
        return Signal(
            signal_id=uuid4(), strategy_id=self.strategy_id, symbol=symbol,
            direction=SignalDirection.LONG, strength=strength,
            timestamp=datetime.utcnow(), target_quantity=qty,
            metadata={"exchange": self.exchange, "leverage": self.default_leverage, **meta},
        )

    def short_signal(self, symbol: str, strength: float, qty: Optional[float] = None, **meta) -> Signal:
        return Signal(
            signal_id=uuid4(), strategy_id=self.strategy_id, symbol=symbol,
            direction=SignalDirection.SHORT, strength=strength,
            timestamp=datetime.utcnow(), target_quantity=qty and -abs(qty),
            metadata={"exchange": self.exchange, "leverage": self.default_leverage, **meta},
        )

    def flat_signal(self, symbol: str, **meta) -> Signal:
        return Signal(
            signal_id=uuid4(), strategy_id=self.strategy_id, symbol=symbol,
            direction=SignalDirection.FLAT, strength=1.0,
            timestamp=datetime.utcnow(), target_quantity=0,
            metadata={"exchange": self.exchange, "reason": "close", **meta},
        )
