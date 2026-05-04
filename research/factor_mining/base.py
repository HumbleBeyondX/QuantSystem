"""CrossSectionalFactor base class + decorator-based registry.

A factor in this framework is a class that, given a DataPanel, returns
a wide DataFrame (date × ticker) of factor values. The values are RAW —
the evaluator/strategy decides whether to z-score, rank, neutralize.

Convention: HIGHER raw value = MORE attractive long. Sign-flip inside the
factor's compute() if the natural signal is "low = good" (e.g. low vol).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Type

import pandas as pd

from research.factor_mining.data_panel import DataPanel


class CrossSectionalFactor(ABC):
    """Base class for cross-sectional equity / multi-asset factors.

    Subclasses must define the class attributes and implement compute().
    """

    # Class attributes (override in subclass)
    name: str = ""
    category: str = ""
    universe_type: str = "equity"   # 'etf' | 'equity' | 'multi' | 'crypto'
    lookback_days: int = 21
    description: str = ""

    @abstractmethod
    def compute(self, panel: DataPanel) -> pd.DataFrame:
        """Compute factor values.

        Args:
            panel: DataPanel with all required OHLCV.

        Returns:
            Wide DataFrame: index = date, columns = tickers, values = factor.
            Higher value = more attractive long (sign-flip natural-low-good factors).
        """
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r} category={self.category!r}>"


# ────────────────────────────────────────────────────────────
# Registry
# ────────────────────────────────────────────────────────────

_REGISTRY: Dict[str, Type[CrossSectionalFactor]] = {}


def register_factor(cls: Type[CrossSectionalFactor]) -> Type[CrossSectionalFactor]:
    """Decorator: register a factor class with the global registry.

    Usage:
        @register_factor
        class Mom_12_1(CrossSectionalFactor):
            name = "mom_12_1"
            category = "momentum"
            ...
    """
    if not issubclass(cls, CrossSectionalFactor):
        raise TypeError(f"{cls.__name__} must subclass CrossSectionalFactor")
    if not cls.name:
        raise ValueError(f"{cls.__name__} needs a non-empty `name` class attribute")
    if cls.name in _REGISTRY:
        raise ValueError(
            f"Factor name '{cls.name}' already registered as {_REGISTRY[cls.name].__name__}"
        )
    _REGISTRY[cls.name] = cls
    return cls


def get_factor(name: str) -> CrossSectionalFactor:
    if name not in _REGISTRY:
        raise KeyError(f"Factor '{name}' not registered. Known: {sorted(_REGISTRY)}")
    return _REGISTRY[name]()


def get_all_factors(
    category: Optional[str] = None,
    universe_type: Optional[str] = None,
) -> List[CrossSectionalFactor]:
    """Instantiate all registered factors, optionally filtered."""
    # Trigger imports so @register_factor runs
    from research.factor_mining.factors import (  # noqa: F401
        momentum,
        volatility,
        reversal,
        quality,
        volume,
        microstructure,
        academic,
        crash_risk,
        composite,
    )

    factors = [cls() for cls in _REGISTRY.values()]
    if category:
        factors = [f for f in factors if f.category == category]
    if universe_type:
        factors = [f for f in factors if f.universe_type in (universe_type, "multi")]
    return factors


def list_factor_names() -> List[str]:
    """Names without instantiation."""
    return sorted(_REGISTRY.keys())
