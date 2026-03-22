"""Strategy base class: the contract all strategies must follow.

Every strategy in the system (crypto, equity, options) implements this ABC.
The same interface is used for both live trading and backtesting.

Lifecycle:
    1. initialize() - called once at startup, load parameters
    2. on_data() - called on each new market data event
    3. generate_signals() - produce trading signals
    4. calculate_target_positions() - convert signals to target positions
    5. on_fill() - called when an order is filled
    6. shutdown() - called on teardown
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from core.enums import AssetClass, SignalDirection
from core.models import Bar, Fill, OrderBookSnapshot, Signal, TargetPosition, Tick


class StrategyState(Enum):
    """Lifecycle state of a strategy."""

    INITIALIZING = "initializing"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class StrategyConfig:
    """Configuration loaded from YAML for a strategy."""

    strategy_id: str
    name: str = ""
    asset_class: AssetClass = AssetClass.CRYPTO
    symbols: List[str] = field(default_factory=list)
    parameters: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    mode: str = "paper"  # "paper" | "live" | "backtest"


class StrategyBase(ABC):
    """Abstract base class for all trading strategies.

    Subclasses must implement:
        - on_bar() and/or on_tick() and/or on_orderbook()
        - generate_signals()

    Optional overrides:
        - initialize(), shutdown(), on_fill()
        - calculate_target_positions() (default just converts signals)
    """

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.strategy_id = config.strategy_id
        self.symbols = config.symbols
        self.params = config.parameters
        self.state = StrategyState.INITIALIZING

        # Internal state
        self._signals: List[Signal] = []
        self._positions: Dict[str, float] = {}  # symbol -> current qty
        self._last_bars: Dict[str, Bar] = {}
        self._metrics: Dict[str, Any] = {
            "signals_generated": 0,
            "trades_executed": 0,
            "start_time": None,
        }

    # ============================================================
    # Lifecycle Methods
    # ============================================================

    def initialize(self) -> None:
        """Called once at startup. Override to load models, warm up indicators, etc."""
        self.state = StrategyState.RUNNING
        self._metrics["start_time"] = datetime.utcnow()

    def shutdown(self) -> None:
        """Called on teardown. Override to save state, close connections, etc."""
        self.state = StrategyState.STOPPED

    # ============================================================
    # Data Handlers (override the ones relevant to your strategy)
    # ============================================================

    def on_bar(self, symbol: str, bar: Bar) -> None:
        """Called on each new OHLCV bar. Override for bar-based strategies."""
        self._last_bars[symbol] = bar

    def on_tick(self, symbol: str, tick: Tick) -> None:
        """Called on each new tick. Override for tick-based strategies (scalping)."""
        pass

    def on_orderbook(self, symbol: str, orderbook: OrderBookSnapshot) -> None:
        """Called on orderbook update. Override for orderbook strategies."""
        pass

    # ============================================================
    # Signal Generation (must implement)
    # ============================================================

    @abstractmethod
    def generate_signals(self) -> List[Signal]:
        """Generate trading signals based on current state.

        Returns:
            List of Signal objects with direction and strength.
        """
        ...

    # ============================================================
    # Position Targeting
    # ============================================================

    def calculate_target_positions(self) -> List[TargetPosition]:
        """Convert signals to target positions.

        Default implementation: map signals directly to target positions.
        Override for more sophisticated position sizing (Kelly, risk budget, etc.).
        """
        signals = self.generate_signals()
        self._signals.extend(signals)
        self._metrics["signals_generated"] += len(signals)

        targets: List[TargetPosition] = []
        for signal in signals:
            # Map direction to position sign: LONG=+1, SHORT=-1, FLAT=0
            if signal.direction == SignalDirection.LONG:
                qty_sign = 1.0
            elif signal.direction == SignalDirection.SHORT:
                qty_sign = -1.0
            else:
                qty_sign = 0.0  # FLAT → close position

            target_qty = signal.target_quantity if signal.target_quantity else qty_sign * signal.strength

            targets.append(
                TargetPosition(
                    symbol=signal.symbol,
                    strategy_id=self.strategy_id,
                    target_quantity=target_qty,
                    target_weight=signal.strength,
                    urgency=0.5,
                    timestamp=signal.timestamp,
                )
            )

        return targets

    # ============================================================
    # Execution Feedback
    # ============================================================

    def on_fill(self, fill: Fill) -> None:
        """Called when an order is filled. Update internal position tracking."""
        current = self._positions.get(fill.symbol, 0.0)
        if fill.side.value == "BUY":
            self._positions[fill.symbol] = current + fill.quantity
        else:
            self._positions[fill.symbol] = current - fill.quantity
        self._metrics["trades_executed"] += 1

    # ============================================================
    # Helpers
    # ============================================================

    def get_position(self, symbol: str) -> float:
        """Get current position quantity for a symbol."""
        return self._positions.get(symbol, 0.0)

    def is_flat(self, symbol: str) -> bool:
        """Check if position is flat (no exposure)."""
        return abs(self.get_position(symbol)) < 1e-10

    def get_param(self, key: str, default: Any = None) -> Any:
        """Get a strategy parameter by key."""
        return self.params.get(key, default)

    @property
    def recent_signals(self) -> List[Signal]:
        """Get recent signals (last 100)."""
        return self._signals[-100:]

    @property
    def metrics(self) -> Dict[str, Any]:
        """Get strategy metrics."""
        return self._metrics.copy()

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} id={self.strategy_id} state={self.state.value}>"
