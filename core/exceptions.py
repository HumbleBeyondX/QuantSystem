"""Custom exception hierarchy for the QuantSystem."""


class QuantSystemError(Exception):
    """Base exception for all QuantSystem errors."""


# --- Data Layer Exceptions ---


class DataError(QuantSystemError):
    """Base exception for data-related errors."""


class ConnectorError(DataError):
    """Error connecting to or streaming from a data source."""


class DataValidationError(DataError):
    """Data failed quality validation checks."""


class StorageError(DataError):
    """Error reading from or writing to storage."""


# --- Strategy Layer Exceptions ---


class StrategyError(QuantSystemError):
    """Base exception for strategy-related errors."""


class StrategyConfigError(StrategyError):
    """Invalid strategy configuration."""


class SignalGenerationError(StrategyError):
    """Error generating trading signals."""


# --- Risk Layer Exceptions ---


class RiskError(QuantSystemError):
    """Base exception for risk management errors."""


class PreTradeCheckFailed(RiskError):
    """Order rejected by pre-trade risk checks."""

    def __init__(self, reason: str, order_id: str = ""):
        self.reason = reason
        self.order_id = order_id
        super().__init__(f"Pre-trade check failed for order {order_id}: {reason}")


class CircuitBreakerTriggered(RiskError):
    """Circuit breaker has been activated."""

    def __init__(self, trigger: str, strategy_id: str = ""):
        self.trigger = trigger
        self.strategy_id = strategy_id
        super().__init__(f"Circuit breaker triggered: {trigger} (strategy={strategy_id})")


# --- Execution Layer Exceptions ---


class ExecutionError(QuantSystemError):
    """Base exception for execution-related errors."""


class OrderRejected(ExecutionError):
    """Order was rejected by the broker."""


class InsufficientFunds(ExecutionError):
    """Insufficient funds/margin to place order."""


class BrokerConnectionError(ExecutionError):
    """Lost connection to broker."""


# --- Configuration Exceptions ---


class ConfigError(QuantSystemError):
    """Invalid or missing configuration."""
