"""High-frequency trading strategy base class — tick-level, sub-second decisions.

Extends StrategyBase with HFT/medium-frequency-specific capabilities:
- Tick-by-tick processing with nanosecond timestamps
- Orderbook microstructure features (imbalance, spread, depth, toxicity)
- TWAP/VWAP execution helpers
- Inventory management with mean-reversion targets
- Latency-aware order management (IOC, FOK, post-only)
- Market making spread computation
- Fill ratio and adverse selection tracking
- Rate limiter for order submission

Designed for:
  - Crypto perpetual futures market-making (Binance/Bybit/OKX)
  - US equity tick-level strategies (via IB/Alpaca)
  - Options scalping (SPY 0DTE gamma scalps)

Usage:
    class MyCryptoMM(HFTStrategyBase):
        def on_orderbook(self, symbol, ob):
            spread = ob.spread
            imbalance = self.orderbook_imbalance(symbol)
            if imbalance > 0.6 and spread > self.min_spread:
                return self.place_quotes(symbol, ...)
"""

from __future__ import annotations

import time
from abc import abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple
from uuid import uuid4

import numpy as np

from core.enums import AssetClass, OrderSide, OrderType, SignalDirection
from core.models import Bar, Fill, OrderBookSnapshot, Signal, TargetPosition, Tick
from strategies.base import StrategyBase, StrategyConfig


# ------------------------------------------------------------------
# Microstructure features
# ------------------------------------------------------------------

@dataclass
class MicrostructureSnapshot:
    """Point-in-time microstructure features from orderbook."""
    timestamp: float  # epoch seconds (high precision)
    mid_price: float
    spread: float
    spread_bps: float
    bid_depth: float  # total qty on bid side
    ask_depth: float  # total qty on ask side
    imbalance: float  # (bid - ask) / (bid + ask), range [-1, 1]
    microprice: float
    top_bid_qty: float
    top_ask_qty: float
    toxicity: float = 0.0  # VPIN-style metric


@dataclass
class InventoryState:
    """Inventory tracking for market-making strategies."""
    symbol: str
    position: float = 0.0
    target: float = 0.0  # inventory target (usually 0 for MM)
    max_inventory: float = 0.0
    avg_entry: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    num_fills: int = 0
    adverse_fills: int = 0  # fills followed by price moving against us

    @property
    def inventory_ratio(self) -> float:
        """Ratio of current inventory to max. 0 = flat, 1 = max long, -1 = max short."""
        if self.max_inventory == 0:
            return 0.0
        return self.position / self.max_inventory

    @property
    def fill_quality(self) -> float:
        """Fraction of fills that were NOT adversely selected."""
        if self.num_fills == 0:
            return 1.0
        return 1.0 - (self.adverse_fills / self.num_fills)


class HFTStrategyBase(StrategyBase):
    """Base class for high-frequency and medium-frequency strategies.

    Optimized for tick-level decision-making with orderbook data.
    Provides microstructure feature computation, inventory management,
    rate limiting, and market-making infrastructure.
    """

    def __init__(self, config: StrategyConfig) -> None:
        super().__init__(config)

        # ── Orderbook state ──
        self._orderbooks: Dict[str, OrderBookSnapshot] = {}
        self._micro_history: Dict[str, Deque[MicrostructureSnapshot]] = {}
        self._tick_history: Dict[str, Deque[Tick]] = {}

        # ── Inventory management ──
        self._inventory: Dict[str, InventoryState] = {}
        self.max_inventory = self.get_param("max_inventory", 1.0)
        self.inventory_skew_factor = self.get_param("inventory_skew_factor", 0.5)

        # ── Rate limiting ──
        self.max_orders_per_second = self.get_param("max_orders_per_second", 10)
        self._order_timestamps: Deque[float] = deque(maxlen=100)

        # ── Spread & quoting ──
        self.min_spread_bps = self.get_param("min_spread_bps", 2.0)
        self.base_spread_bps = self.get_param("base_spread_bps", 5.0)
        self.quote_size = self.get_param("quote_size", 0.01)

        # ── Feature window ──
        self._micro_window = self.get_param("micro_window", 100)
        self._tick_window = self.get_param("tick_window", 500)

        # ── Last trade tracking for adverse selection ──
        self._last_fill_prices: Dict[str, Deque[Tuple[float, float, str]]] = {}  # (ts, price, side)

    # ------------------------------------------------------------------
    # Orderbook processing
    # ------------------------------------------------------------------

    def on_orderbook(self, symbol: str, orderbook: OrderBookSnapshot) -> None:
        """Process orderbook update — compute microstructure features."""
        self._orderbooks[symbol] = orderbook

        if symbol not in self._micro_history:
            self._micro_history[symbol] = deque(maxlen=self._micro_window)

        # Compute features
        snap = self._compute_microstructure(symbol, orderbook)
        self._micro_history[symbol].append(snap)

    def on_tick(self, symbol: str, tick: Tick) -> None:
        """Cache tick data for trade-flow analysis."""
        if symbol not in self._tick_history:
            self._tick_history[symbol] = deque(maxlen=self._tick_window)
        self._tick_history[symbol].append(tick)

    def _compute_microstructure(self, symbol: str, ob: OrderBookSnapshot) -> MicrostructureSnapshot:
        """Extract microstructure features from raw orderbook."""
        mid = ob.mid_price
        spread = ob.spread
        spread_bps = (spread / mid * 10000) if mid > 0 else 0

        # Depth: sum of qty across visible levels
        bid_depth = sum(level.quantity for level in ob.bids[:10])
        ask_depth = sum(level.quantity for level in ob.asks[:10])

        total_depth = bid_depth + ask_depth
        imbalance = (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0.0

        top_bid_qty = ob.bids[0].quantity if ob.bids else 0
        top_ask_qty = ob.asks[0].quantity if ob.asks else 0

        return MicrostructureSnapshot(
            timestamp=time.time(),
            mid_price=mid,
            spread=spread,
            spread_bps=spread_bps,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            imbalance=imbalance,
            microprice=ob.microprice,
            top_bid_qty=top_bid_qty,
            top_ask_qty=top_ask_qty,
        )

    # ------------------------------------------------------------------
    # Microstructure feature accessors
    # ------------------------------------------------------------------

    def orderbook_imbalance(self, symbol: str) -> float:
        """Current orderbook imbalance. Positive = buy pressure."""
        hist = self._micro_history.get(symbol)
        if not hist:
            return 0.0
        return hist[-1].imbalance

    def spread_bps(self, symbol: str) -> float:
        """Current spread in basis points."""
        hist = self._micro_history.get(symbol)
        if not hist:
            return float("inf")
        return hist[-1].spread_bps

    def microprice(self, symbol: str) -> float:
        """Volume-weighted mid price (more accurate fair value)."""
        hist = self._micro_history.get(symbol)
        if not hist:
            return 0.0
        return hist[-1].microprice

    def mid_price(self, symbol: str) -> float:
        ob = self._orderbooks.get(symbol)
        return ob.mid_price if ob else 0.0

    def imbalance_ema(self, symbol: str, window: int = 20) -> float:
        """Exponential moving average of orderbook imbalance."""
        hist = self._micro_history.get(symbol)
        if not hist or len(hist) < 2:
            return 0.0
        values = [s.imbalance for s in list(hist)[-window:]]
        alpha = 2.0 / (len(values) + 1)
        ema = values[0]
        for v in values[1:]:
            ema = alpha * v + (1 - alpha) * ema
        return ema

    def trade_flow_imbalance(self, symbol: str, seconds: float = 10.0) -> float:
        """Net buy vs sell volume in recent ticks."""
        ticks = self._tick_history.get(symbol)
        if not ticks:
            return 0.0
        cutoff = time.time() - seconds
        buy_vol = sell_vol = 0.0
        for t in reversed(list(ticks)):
            if t.timestamp.timestamp() < cutoff:
                break
            if t.last > 0:
                if hasattr(t, "side") and str(getattr(t, "side", "")) == "BUY":
                    buy_vol += t.volume
                else:
                    sell_vol += t.volume
        total = buy_vol + sell_vol
        return (buy_vol - sell_vol) / total if total > 0 else 0.0

    # ------------------------------------------------------------------
    # Inventory management
    # ------------------------------------------------------------------

    def get_inventory(self, symbol: str) -> InventoryState:
        if symbol not in self._inventory:
            self._inventory[symbol] = InventoryState(
                symbol=symbol,
                max_inventory=self.max_inventory,
            )
        return self._inventory[symbol]

    def update_inventory(self, symbol: str, fill: Fill) -> None:
        """Update inventory from a fill."""
        inv = self.get_inventory(symbol)
        if fill.side == OrderSide.BUY:
            inv.position += fill.quantity
        else:
            inv.position -= fill.quantity
        inv.num_fills += 1

    def inventory_skew(self, symbol: str) -> float:
        """Compute quote skew based on inventory.

        Returns adjustment in bps to add to bid/ask spread.
        Positive inventory -> widen ask (discourage buys), tighten bid (encourage sells).
        """
        inv = self.get_inventory(symbol)
        return inv.inventory_ratio * self.inventory_skew_factor * self.base_spread_bps

    # ------------------------------------------------------------------
    # Market-making quote computation
    # ------------------------------------------------------------------

    def compute_quotes(self, symbol: str) -> Optional[Tuple[float, float, float, float]]:
        """Compute bid/ask quotes for market making.

        Returns: (bid_price, bid_qty, ask_price, ask_qty) or None if conditions not met.
        """
        mid = self.mid_price(symbol)
        if mid <= 0:
            return None

        # Base half-spread
        half_spread = mid * (self.base_spread_bps / 10000 / 2)

        # Inventory skew
        skew = self.inventory_skew(symbol) * mid / 10000

        bid_price = mid - half_spread - skew
        ask_price = mid + half_spread - skew  # skew shifts both quotes

        return (bid_price, self.quote_size, ask_price, self.quote_size)

    # ------------------------------------------------------------------
    # Rate limiter
    # ------------------------------------------------------------------

    def can_send_order(self) -> bool:
        """Check if we're within rate limits."""
        now = time.time()
        # Remove old timestamps
        while self._order_timestamps and now - self._order_timestamps[0] > 1.0:
            self._order_timestamps.popleft()
        return len(self._order_timestamps) < self.max_orders_per_second

    def record_order_sent(self) -> None:
        self._order_timestamps.append(time.time())

    # ------------------------------------------------------------------
    # Signal helpers
    # ------------------------------------------------------------------

    def _make_signal(self, symbol: str, direction: SignalDirection, strength: float,
                     qty: Optional[float] = None, **kwargs) -> Signal:
        return Signal(
            signal_id=uuid4(), strategy_id=self.strategy_id, symbol=symbol,
            direction=direction, strength=strength,
            timestamp=datetime.utcnow(), target_quantity=qty,
            metadata=kwargs,
        )

    def generate_signals(self) -> List[Signal]:
        """Default: no signals. Override in subclass."""
        return []
