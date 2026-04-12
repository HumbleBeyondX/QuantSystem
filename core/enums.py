"""Enumerations used across the entire QuantSystem."""

from enum import Enum, auto


class AssetClass(str, Enum):
    CRYPTO = "crypto"
    CRYPTO_PERP = "crypto_perp"
    CRYPTO_SPOT = "crypto_spot"
    EQUITY = "equity"
    OPTIONS = "options"


class TimeFrame(str, Enum):
    TICK = "tick"
    S1 = "1s"
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    TRAILING_STOP = "TRAILING_STOP"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class StrategyState(str, Enum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class SignalDirection(int, Enum):
    LONG = 1
    SHORT = -1
    FLAT = 0


class DataType(str, Enum):
    TICK = "tick"
    TRADE = "trade"
    BAR = "bar"
    ORDERBOOK = "orderbook"
    QUOTE = "quote"
    OHLCV = "ohlcv"


class MarketRegime(str, Enum):
    BULL = "bull"
    BEAR = "bear"
    RANGE = "range"
    HIGH_VOL = "high_vol"
    LOW_VOL = "low_vol"
