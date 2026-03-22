"""Feature Store interface and registry.

Central registry for all features (technical, microstructure, fundamental, sentiment).
Supports both batch computation (for backtesting) and streaming computation (for live).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Set

import pandas as pd

from core.logging import get_logger

logger = get_logger(__name__)

# Type for feature computation function: takes DataFrame, returns Series or DataFrame
FeatureFunc = Callable[[pd.DataFrame], pd.Series | pd.DataFrame]


class FeatureDefinition:
    """Metadata about a registered feature."""

    def __init__(
        self,
        name: str,
        func: FeatureFunc,
        category: str = "technical",
        description: str = "",
        dependencies: Optional[List[str]] = None,
        lookback: int = 0,
    ) -> None:
        self.name = name
        self.func = func
        self.category = category
        self.description = description
        self.dependencies = dependencies or []
        self.lookback = lookback  # Minimum bars needed


class FeatureStore:
    """Central feature registry and computation engine.

    Usage:
        store = FeatureStore()
        store.register("rsi_14", compute_rsi_14, category="technical", lookback=14)
        features_df = store.compute(ohlcv_df, ["rsi_14", "ema_21", "vwap"])
    """

    def __init__(self) -> None:
        self._registry: Dict[str, FeatureDefinition] = {}
        self._cache: Dict[str, pd.Series] = {}

    def register(
        self,
        name: str,
        func: FeatureFunc,
        category: str = "technical",
        description: str = "",
        dependencies: Optional[List[str]] = None,
        lookback: int = 0,
    ) -> None:
        """Register a feature computation function."""
        self._registry[name] = FeatureDefinition(
            name=name,
            func=func,
            category=category,
            description=description,
            dependencies=dependencies,
            lookback=lookback,
        )

    def register_batch(self, features: Dict[str, FeatureFunc], category: str = "technical") -> None:
        """Register multiple features at once."""
        for name, func in features.items():
            self.register(name, func, category=category)

    def compute(
        self,
        df: pd.DataFrame,
        feature_names: Optional[List[str]] = None,
        use_cache: bool = False,
    ) -> pd.DataFrame:
        """Compute features and return as a DataFrame.

        Args:
            df: Input OHLCV DataFrame (must have open, high, low, close, volume columns)
            feature_names: List of features to compute. None = all registered.
            use_cache: Whether to use cached results.

        Returns:
            DataFrame with original columns + computed feature columns.
        """
        if feature_names is None:
            feature_names = list(self._registry.keys())

        result = df.copy()

        # Resolve dependencies (topological order)
        to_compute = self._resolve_dependencies(feature_names)

        for fname in to_compute:
            if fname not in self._registry:
                logger.warning("feature_not_registered", feature=fname)
                continue

            if use_cache and fname in self._cache:
                result[fname] = self._cache[fname]
                continue

            defn = self._registry[fname]
            try:
                output = defn.func(result)
                if isinstance(output, pd.DataFrame):
                    # Multi-column feature (e.g., MACD returns macd, signal, hist)
                    for col in output.columns:
                        result[col] = output[col]
                else:
                    result[fname] = output

                if use_cache:
                    self._cache[fname] = output

            except Exception as e:
                logger.error("feature_compute_error", feature=fname, error=str(e))

        return result

    def compute_single(self, df: pd.DataFrame, feature_name: str) -> Optional[pd.Series]:
        """Compute a single feature."""
        if feature_name not in self._registry:
            return None
        try:
            return self._registry[feature_name].func(df)
        except Exception as e:
            logger.error("feature_compute_error", feature=feature_name, error=str(e))
            return None

    def _resolve_dependencies(self, feature_names: List[str]) -> List[str]:
        """Resolve feature dependencies in topological order."""
        resolved: List[str] = []
        seen: Set[str] = set()

        def _visit(name: str) -> None:
            if name in seen:
                return
            seen.add(name)
            if name in self._registry:
                for dep in self._registry[name].dependencies:
                    _visit(dep)
            resolved.append(name)

        for name in feature_names:
            _visit(name)
        return resolved

    @property
    def available_features(self) -> List[str]:
        """List all registered feature names."""
        return list(self._registry.keys())

    def get_features_by_category(self, category: str) -> List[str]:
        """List features in a specific category."""
        return [
            name for name, defn in self._registry.items()
            if defn.category == category
        ]

    def get_min_lookback(self, feature_names: List[str]) -> int:
        """Get the minimum lookback period needed for a set of features."""
        return max(
            (self._registry[n].lookback for n in feature_names if n in self._registry),
            default=0,
        )

    def clear_cache(self) -> None:
        """Clear the feature cache."""
        self._cache.clear()

    def info(self) -> Dict[str, Any]:
        """Return registry info."""
        return {
            "total_features": len(self._registry),
            "categories": list(set(d.category for d in self._registry.values())),
            "features": {
                name: {
                    "category": d.category,
                    "lookback": d.lookback,
                    "description": d.description,
                }
                for name, d in self._registry.items()
            },
        }
