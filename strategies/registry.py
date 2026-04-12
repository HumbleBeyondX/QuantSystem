"""Strategy registry: discover, register, and instantiate strategies by name."""

from __future__ import annotations

from typing import Dict, List, Optional, Type

from core.logging import get_logger
from strategies.base import StrategyBase, StrategyConfig

logger = get_logger(__name__)

# Global strategy registry
_REGISTRY: Dict[str, Type[StrategyBase]] = {}


def register_strategy(name: str):
    """Decorator to register a strategy class.

    Usage:
        @register_strategy("crypto_scalping")
        class ScalpingStrategy(StrategyBase):
            ...
    """
    def decorator(cls: Type[StrategyBase]) -> Type[StrategyBase]:
        _REGISTRY[name] = cls
        logger.debug("strategy_registered", name=name, cls=cls.__name__)
        return cls
    return decorator


def get_strategy_class(name: str) -> Optional[Type[StrategyBase]]:
    """Get a strategy class by registered name."""
    return _REGISTRY.get(name)


def create_strategy(name: str, config: StrategyConfig) -> StrategyBase:
    """Instantiate a strategy by name with config."""
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown strategy: {name}. Available: {list(_REGISTRY.keys())}")
    return cls(config)


def list_strategies() -> List[str]:
    """List all registered strategy names."""
    return list(_REGISTRY.keys())


def discover_strategies() -> None:
    """Import all strategy modules to trigger @register_strategy decorators."""
    import importlib

    modules = [
        "strategies.crypto.scalping_strategy",
        "strategies.crypto.breakout_strategy",
        "strategies.crypto.orderbook_strategy",
        "strategies.crypto.event_strategy",
        "strategies.crypto.swing_strategy",
        "strategies.crypto.momentum_strategy",
        "strategies.equity.factor_strategy",
        "strategies.equity.timing_strategy",
        "strategies.equity.long_short_strategy",
        "strategies.options.delta_neutral_strategy",
        "strategies.options.gamma_scalp_strategy",
        "strategies.crypto.macro_cycle_strategy",
        "strategies.crypto.sniper_strategy",
        "strategies.options.spy_iron_condor_strategy",
        "strategies.crypto.test_hft_bot_strategy",
        "strategies.options.spy_wheel_strategy",
    ]

    for module_path in modules:
        try:
            importlib.import_module(module_path)
        except ImportError as e:
            logger.warning("strategy_import_failed", module=module_path, error=str(e))
