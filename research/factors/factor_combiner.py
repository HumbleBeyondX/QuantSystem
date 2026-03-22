"""Factor combination strategies.

Provides methods for combining multiple alpha factors into a single composite
signal using equal weighting, IC-based weighting, rank-based combination, or
machine-learning approaches.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

try:
    from sklearn.linear_model import RidgeCV
    from sklearn.preprocessing import StandardScaler

    _HAS_SKLEARN = True
except ImportError:
    _HAS_SKLEARN = False


class FactorCombiner:
    """Combine multiple alpha factors into a single composite signal.

    All combination methods accept a dictionary mapping factor names to
    ``pd.DataFrame`` instances of shape ``(dates, symbols)`` and return a
    single ``pd.DataFrame`` of the same shape.
    """

    # ------------------------------------------------------------------
    # Equal weight
    # ------------------------------------------------------------------

    @staticmethod
    def equal_weight(factors: Dict[str, pd.DataFrame]) -> pd.DataFrame:
        """Combine factors using equal weights.

        Each factor is first cross-sectionally z-scored per period, then the
        simple average is taken.

        Args:
            factors: Mapping of factor name to factor DataFrame.

        Returns:
            Combined factor DataFrame.
        """
        standardized = []
        for name, df in factors.items():
            z = df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1), axis=0)
            standardized.append(z)

        stacked = np.stack([s.values for s in standardized], axis=0)
        combined = pd.DataFrame(
            np.nanmean(stacked, axis=0),
            index=list(factors.values())[0].index,
            columns=list(factors.values())[0].columns,
        )
        return combined

    # ------------------------------------------------------------------
    # IC-weighted
    # ------------------------------------------------------------------

    @staticmethod
    def ic_weighted(
        factors: Dict[str, pd.DataFrame],
        ic_scores: Dict[str, float],
    ) -> pd.DataFrame:
        """Combine factors weighted by their information coefficients.

        Factors with higher absolute IC receive proportionally more weight.

        Args:
            factors: Mapping of factor name to factor DataFrame.
            ic_scores: Mapping of factor name to IC value.

        Returns:
            IC-weighted combined factor DataFrame.
        """
        total_ic = sum(abs(v) for v in ic_scores.values())
        if total_ic == 0:
            return FactorCombiner.equal_weight(factors)

        weights = {k: abs(v) / total_ic for k, v in ic_scores.items()}

        standardized: List[pd.DataFrame] = []
        weight_list: List[float] = []
        for name, df in factors.items():
            z = df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1), axis=0)
            standardized.append(z)
            weight_list.append(weights.get(name, 0.0))

        stacked = np.stack([s.values for s in standardized], axis=0)
        w = np.array(weight_list).reshape(-1, 1, 1)
        combined = pd.DataFrame(
            np.nansum(stacked * w, axis=0),
            index=list(factors.values())[0].index,
            columns=list(factors.values())[0].columns,
        )
        return combined

    # ------------------------------------------------------------------
    # Rank-based combination
    # ------------------------------------------------------------------

    @staticmethod
    def rank_combine(factors: Dict[str, pd.DataFrame]) -> pd.DataFrame:
        """Combine factors using cross-sectional ranks.

        Each factor is converted to percentile ranks per period, then the
        simple average rank is computed.

        Args:
            factors: Mapping of factor name to factor DataFrame.

        Returns:
            Rank-combined factor DataFrame.
        """
        ranked = []
        for name, df in factors.items():
            ranked.append(df.rank(axis=1, pct=True))

        stacked = np.stack([r.values for r in ranked], axis=0)
        combined = pd.DataFrame(
            np.nanmean(stacked, axis=0),
            index=list(factors.values())[0].index,
            columns=list(factors.values())[0].columns,
        )
        return combined

    # ------------------------------------------------------------------
    # ML-based combination
    # ------------------------------------------------------------------

    @staticmethod
    def ml_combine(
        factors: Dict[str, pd.DataFrame],
        returns: pd.DataFrame,
        model: Optional[object] = None,
    ) -> pd.DataFrame:
        """Combine factors using a machine-learning model.

        By default a :class:`sklearn.linear_model.RidgeCV` is fitted on the
        stacked factor panel to predict forward returns.  A custom sklearn-
        compatible estimator can be passed via *model*.

        Args:
            factors: Mapping of factor name to factor DataFrame.
            returns: DataFrame of forward returns with the same index and
                columns as the factor DataFrames.
            model: Optional sklearn-compatible estimator.  Must implement
                ``fit(X, y)`` and ``predict(X)``.

        Returns:
            ML-predicted combined signal as a DataFrame.

        Raises:
            ImportError: If scikit-learn is not installed.
        """
        if not _HAS_SKLEARN:
            raise ImportError(
                "scikit-learn is required for ml_combine. "
                "Install it with: pip install scikit-learn"
            )

        factor_names = list(factors.keys())
        reference = list(factors.values())[0]

        # Build a long-format feature matrix
        records = []
        for date in reference.index:
            for symbol in reference.columns:
                row = {"date": date, "symbol": symbol}
                valid = True
                for fn in factor_names:
                    val = factors[fn].loc[date, symbol] if symbol in factors[fn].columns else np.nan
                    if np.isnan(val):
                        valid = False
                        break
                    row[fn] = val

                if not valid:
                    continue

                ret = returns.loc[date, symbol] if (
                    date in returns.index and symbol in returns.columns
                ) else np.nan
                if np.isnan(ret):
                    continue

                row["return"] = ret
                records.append(row)

        panel = pd.DataFrame(records)
        if panel.empty:
            return reference * 0.0

        X = panel[factor_names].values
        y = panel["return"].values

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)

        if model is None:
            model = RidgeCV(alphas=[0.01, 0.1, 1.0, 10.0])

        model.fit(X_scaled, y)  # type: ignore[union-attr]

        # Predict for all date/symbol combinations
        result = reference.copy() * np.nan
        for date in reference.index:
            for symbol in reference.columns:
                vals = []
                valid = True
                for fn in factor_names:
                    v = factors[fn].loc[date, symbol] if symbol in factors[fn].columns else np.nan
                    if np.isnan(v):
                        valid = False
                        break
                    vals.append(v)
                if valid:
                    pred = model.predict(scaler.transform([vals]))[0]  # type: ignore[union-attr]
                    result.loc[date, symbol] = pred

        return result
