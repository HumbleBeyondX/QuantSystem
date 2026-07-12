"""Time-series factor base class — for single-asset crypto research.

A TimeSeriesFactor takes an OHLCV DataFrame indexed by timestamp and returns
a Series of factor values at the same index. This is the right abstraction
for building a library of alpha factors to evaluate on ETH 1H data.

Compared to the existing cross-sectional ``AlphaFactor`` (indexed by symbol),
TimeSeriesFactor is indexed by time — exactly what information-coefficient
analysis against forward returns needs.

Usage::

    @register_factor
    class BodyToRangeRatio(TimeSeriesFactor):
        name = "body_to_range_ratio"
        category = "microstructure"
        lookback_bars = 1
        description = "|close-open| / (high-low) — candle body share of range."

        def compute(self, df: pd.DataFrame) -> pd.Series:
            body = (df["close"] - df["open"]).abs()
            rng = (df["high"] - df["low"]).replace(0, np.nan)
            return body / rng

Convention for factor values:
  * Positive values are bullish (forward returns expected to be positive)
  * Negative values are bearish
  * Zero means neutral
  * NaN allowed for insufficient-history cells (the evaluator drops them)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional, Type

import pandas as pd


@dataclass
class FactorOutput:
    """Wrapper returned by TimeSeriesFactor.compute_with_meta()."""
    name: str
    category: str
    values: pd.Series
    n_non_null: int
    first_non_null_idx: Optional[int] = None


class TimeSeriesFactor(ABC):
    """Time-series alpha factor over a single asset's OHLCV history.

    Class attributes (override in subclass):
        name           — unique snake_case identifier, used as column name
        category       — grouping label: microstructure / time_of_day /
                         volatility_structure / price_action / cross_asset /
                         derivatives
        lookback_bars  — how many prior bars the factor needs. Evaluator
                         skips the first `lookback_bars` rows when scoring.
        description    — short English explanation for the research UI.
    """

    # Subclasses must override these class attributes.
    name: str = ""
    category: str = ""
    lookback_bars: int = 1
    description: str = ""

    @abstractmethod
    def compute(self, df: pd.DataFrame) -> pd.Series:
        """Compute factor values.

        Args:
            df: OHLCV DataFrame with columns
                ['timestamp', 'open', 'high', 'low', 'close', 'volume']
                indexed sequentially (0..n-1). Timestamps are UTC pandas
                Timestamps. Data is assumed pre-sorted ascending.

                May also contain optional columns (e.g. 'btc_close' for
                cross-asset factors) — see the factor's docstring.

        Returns:
            Series of the same length as df. Values may be NaN during the
            warmup period. The name attribute of the Series will be
            overwritten with self.name by compute_with_meta().
        """
        raise NotImplementedError

    def compute_with_meta(self, df: pd.DataFrame) -> FactorOutput:
        """Compute + wrap in FactorOutput with metadata."""
        values = self.compute(df).astype("float64")
        values.name = self.name
        non_null = values.notna().sum()
        first_valid = values.first_valid_index()
        return FactorOutput(
            name=self.name,
            category=self.category,
            values=values,
            n_non_null=int(non_null),
            first_non_null_idx=int(first_valid) if first_valid is not None else None,
        )

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r} category={self.category!r}>"


# ────────────────────────────────────────────────────────────
# Registry
# ────────────────────────────────────────────────────────────

_REGISTRY: Dict[str, Type[TimeSeriesFactor]] = {}


def register_factor(cls: Type[TimeSeriesFactor]) -> Type[TimeSeriesFactor]:
    """Class decorator — adds the factor class to the global registry."""
    if not issubclass(cls, TimeSeriesFactor):
        raise TypeError(f"{cls.__name__} must subclass TimeSeriesFactor")
    if not cls.name:
        raise ValueError(f"{cls.__name__} must define a non-empty `name`")
    if cls.name in _REGISTRY:
        raise ValueError(f"Factor '{cls.name}' already registered")
    _REGISTRY[cls.name] = cls
    return cls


def get_factor(name: str) -> TimeSeriesFactor:
    """Instantiate a registered factor by name."""
    if name not in _REGISTRY:
        raise KeyError(f"Factor '{name}' not registered. Known: {list(_REGISTRY)}")
    return _REGISTRY[name]()


def get_all_factors(category: Optional[str] = None) -> List[TimeSeriesFactor]:
    """Instantiate all registered factors (optionally filtered by category).

    Side effect: importing ``research.factors`` for the first time triggers
    registration of all built-in factors via their module imports.
    """
    # Ensure built-in factor modules are imported so their @register_factor
    # decorators run. Keep this list in sync with factor modules.
    from research.factors import (  # noqa: F401
        microstructure,
        time_of_day,
        volatility_structure,
        price_action,
        cross_asset,
        derivatives,
    )

    factors = [cls() for cls in _REGISTRY.values()]
    if category:
        factors = [f for f in factors if f.category == category]
    return factors


def list_factor_names() -> List[str]:
    """List all registered factor names without instantiating."""
    return list(_REGISTRY.keys())
