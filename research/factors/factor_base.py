"""Abstract base for alpha factors."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import pandas as pd


@dataclass
class FactorMetadata:
    """Metadata describing an alpha factor's properties and configuration.

    Attributes:
        name: Human-readable factor name (e.g. "momentum_12m").
        category: Factor category. One of "momentum", "value", "quality",
            "volatility", "technical".
        universe: Asset universe the factor targets. One of "equity", "crypto",
            "all".
        lookback_days: Number of calendar days of history required to compute
            the factor.
        rebalance_freq: How often the factor is recomputed. One of "daily",
            "weekly", "monthly", "quarterly".
        description: Free-text description of the factor's economic rationale.
    """

    name: str
    category: str  # "momentum", "value", "quality", "volatility", "technical"
    universe: str  # "equity", "crypto", "all"
    lookback_days: int = 252
    rebalance_freq: str = "monthly"
    description: str = ""


class AlphaFactor(ABC):
    """Base class for all alpha factors.

    Subclasses must implement :meth:`compute` and :meth:`validate`.  The base
    class provides common utilities such as neutralization and winsorization
    that are useful across factor implementations.

    Parameters:
        metadata: A :class:`FactorMetadata` instance describing the factor.
    """

    def __init__(self, metadata: FactorMetadata) -> None:
        self.metadata = metadata

    @abstractmethod
    def compute(self, data: pd.DataFrame) -> pd.Series:
        """Compute factor values from input data.

        Args:
            data: DataFrame containing the raw market data required by the
                factor (e.g. OHLCV).  The expected schema depends on the
                concrete implementation.

        Returns:
            A :class:`pd.Series` of factor values indexed by symbol.
        """
        ...

    @abstractmethod
    def validate(self, factor_values: pd.Series) -> bool:
        """Validate computed factor values.

        Checks should include, at minimum:
        - No large clusters of NaN values.
        - Values fall within a reasonable range.

        Args:
            factor_values: The output of :meth:`compute`.

        Returns:
            ``True`` if the factor values pass all validation checks.
        """
        ...

    def neutralize(
        self,
        factor_values: pd.Series,
        groups: Optional[pd.Series] = None,
    ) -> pd.Series:
        """Market- or sector-neutralize factor values.

        When *groups* is provided the neutralization is performed within each
        group (e.g. sector, industry).  Otherwise a simple cross-sectional
        z-score is applied.

        Args:
            factor_values: Raw factor values indexed by symbol.
            groups: Optional grouping variable (e.g. sector labels) aligned
                with *factor_values*.

        Returns:
            Neutralized factor values as a :class:`pd.Series`.
        """
        if groups is not None:
            return factor_values.groupby(groups).transform(
                lambda x: (x - x.mean()) / x.std()
            )
        return (factor_values - factor_values.mean()) / factor_values.std()

    def winsorize(
        self,
        series: pd.Series,
        limits: tuple = (0.01, 0.99),
    ) -> pd.Series:
        """Clip extreme values at the given quantile boundaries.

        Args:
            series: Factor values to winsorize.
            limits: A ``(lower, upper)`` tuple of quantiles.  Values outside
                these bounds are clipped.

        Returns:
            Winsorized :class:`pd.Series`.
        """
        lower = series.quantile(limits[0])
        upper = series.quantile(limits[1])
        return series.clip(lower, upper)
