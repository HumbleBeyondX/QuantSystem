"""Single-factor testing framework.

Provides a comprehensive suite of tests for evaluating the predictive power
and statistical properties of individual alpha factors, including information
coefficient analysis, quintile spreads, and turnover measurement.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .factor_base import AlphaFactor


@dataclass
class ICResult:
    """Container for information coefficient results.

    Attributes:
        ic_mean: Mean Pearson IC across periods.
        ic_std: Standard deviation of IC across periods.
        ir: Information ratio (ic_mean / ic_std).
        rank_ic_mean: Mean Spearman rank IC across periods.
        rank_ic_std: Standard deviation of rank IC across periods.
        ic_series: Full time series of IC values.
        rank_ic_series: Full time series of rank IC values.
    """

    ic_mean: float
    ic_std: float
    ir: float
    rank_ic_mean: float
    rank_ic_std: float
    ic_series: pd.Series
    rank_ic_series: pd.Series


@dataclass
class QuintileResult:
    """Container for quintile analysis results.

    Attributes:
        mean_returns: Mean return for each quintile.
        cumulative_returns: Cumulative return series for each quintile.
        spread: Long-short spread (Q5 - Q1).
        monotonicity: Spearman correlation between quintile rank and mean
            return (1.0 = perfectly monotonic).
    """

    mean_returns: pd.Series
    cumulative_returns: pd.DataFrame
    spread: float
    monotonicity: float


class SingleFactorTest:
    """Framework for running comprehensive single-factor tests.

    This class evaluates an alpha factor's predictive power by computing
    information coefficients, quintile return spreads, turnover, and IC
    decay profiles.

    Parameters:
        n_quantiles: Number of quantile buckets for portfolio analysis.
            Defaults to 5 (quintiles).
    """

    def __init__(self, n_quantiles: int = 5) -> None:
        self.n_quantiles = n_quantiles

    # ------------------------------------------------------------------
    # Information coefficient
    # ------------------------------------------------------------------

    def compute_ic(
        self,
        factor: pd.DataFrame,
        returns: pd.DataFrame,
    ) -> ICResult:
        """Compute cross-sectional information coefficients.

        For each time period the Pearson and Spearman rank correlation between
        the factor values and the subsequent forward returns is calculated.

        Args:
            factor: DataFrame of factor values with shape ``(dates, symbols)``.
            returns: DataFrame of forward returns aligned with *factor*.

        Returns:
            An :class:`ICResult` with summary statistics and the full IC
            time series.
        """
        common_dates = factor.index.intersection(returns.index)
        common_cols = factor.columns.intersection(returns.columns)

        factor_aligned = factor.loc[common_dates, common_cols]
        returns_aligned = returns.loc[common_dates, common_cols]

        ic_values: List[float] = []
        rank_ic_values: List[float] = []

        for date in common_dates:
            f = factor_aligned.loc[date].dropna()
            r = returns_aligned.loc[date].dropna()
            common = f.index.intersection(r.index)
            if len(common) < 5:
                continue
            ic_values.append(f[common].corr(r[common]))
            rank_ic_values.append(f[common].corr(r[common], method="spearman"))

        ic_series = pd.Series(ic_values, index=common_dates[: len(ic_values)])
        rank_ic_series = pd.Series(
            rank_ic_values, index=common_dates[: len(rank_ic_values)]
        )

        ic_mean = float(ic_series.mean())
        ic_std = float(ic_series.std())
        ir = ic_mean / ic_std if ic_std > 0 else 0.0

        return ICResult(
            ic_mean=ic_mean,
            ic_std=ic_std,
            ir=ir,
            rank_ic_mean=float(rank_ic_series.mean()),
            rank_ic_std=float(rank_ic_series.std()),
            ic_series=ic_series,
            rank_ic_series=rank_ic_series,
        )

    # ------------------------------------------------------------------
    # IC decay
    # ------------------------------------------------------------------

    def compute_ic_decay(
        self,
        factor: pd.DataFrame,
        returns: pd.DataFrame,
        lags: Optional[List[int]] = None,
    ) -> pd.DataFrame:
        """Compute IC at various forward-return lags.

        This reveals how quickly a factor's signal decays over time.

        Args:
            factor: DataFrame of factor values ``(dates, symbols)``.
            returns: DataFrame of single-period returns ``(dates, symbols)``.
            lags: List of lag periods to evaluate.  Defaults to
                ``[1, 2, 3, 5, 10, 20]``.

        Returns:
            DataFrame with columns ``["lag", "ic_mean", "rank_ic_mean"]``.
        """
        if lags is None:
            lags = [1, 2, 3, 5, 10, 20]

        records: List[Dict[str, Any]] = []
        for lag in lags:
            forward_returns = returns.shift(-lag)
            result = self.compute_ic(factor, forward_returns)
            records.append(
                {
                    "lag": lag,
                    "ic_mean": result.ic_mean,
                    "rank_ic_mean": result.rank_ic_mean,
                }
            )

        return pd.DataFrame(records)

    # ------------------------------------------------------------------
    # Quintile analysis
    # ------------------------------------------------------------------

    def quintile_analysis(
        self,
        factor: pd.DataFrame,
        returns: pd.DataFrame,
    ) -> QuintileResult:
        """Analyse returns by factor quintile.

        For each period, assets are sorted into *n_quantiles* buckets based on
        their factor value.  Mean returns per bucket are then computed.

        Args:
            factor: DataFrame of factor values ``(dates, symbols)``.
            returns: DataFrame of forward returns aligned with *factor*.

        Returns:
            A :class:`QuintileResult` with mean returns, cumulative returns,
            long-short spread, and monotonicity score.
        """
        common_dates = factor.index.intersection(returns.index)
        common_cols = factor.columns.intersection(returns.columns)

        quantile_returns: Dict[int, List[float]] = {
            q: [] for q in range(1, self.n_quantiles + 1)
        }

        for date in common_dates:
            f = factor.loc[date, common_cols].dropna()
            r = returns.loc[date, common_cols].dropna()
            common = f.index.intersection(r.index)
            if len(common) < self.n_quantiles:
                continue

            quantile_labels = pd.qcut(
                f[common], self.n_quantiles, labels=False, duplicates="drop"
            )
            for q in range(int(quantile_labels.max()) + 1):
                bucket = q + 1
                mask = quantile_labels == q
                if mask.sum() > 0 and bucket <= self.n_quantiles:
                    quantile_returns[bucket].append(float(r[common][mask].mean()))

        mean_returns = pd.Series(
            {q: np.mean(v) if v else np.nan for q, v in quantile_returns.items()}
        )

        cumulative = pd.DataFrame(
            {q: np.cumprod(1 + np.array(v)) - 1 if v else [] for q, v in quantile_returns.items()}
        )

        spread = float(mean_returns.iloc[-1] - mean_returns.iloc[0])

        # Monotonicity: Spearman correlation of quintile rank vs mean return.
        from scipy.stats import spearmanr

        valid = mean_returns.dropna()
        if len(valid) >= 3:
            monotonicity, _ = spearmanr(
                np.arange(len(valid)), valid.values
            )
        else:
            monotonicity = np.nan

        return QuintileResult(
            mean_returns=mean_returns,
            cumulative_returns=cumulative,
            spread=spread,
            monotonicity=float(monotonicity),
        )

    # ------------------------------------------------------------------
    # Turnover analysis
    # ------------------------------------------------------------------

    def turnover_analysis(self, factor: pd.DataFrame) -> pd.Series:
        """Compute factor portfolio turnover over time.

        Turnover is defined as the mean absolute change in cross-sectional
        rank from one period to the next, normalized to [0, 1].

        Args:
            factor: DataFrame of factor values ``(dates, symbols)``.

        Returns:
            A :class:`pd.Series` of turnover values indexed by date (excluding
            the first period).
        """
        ranks = factor.rank(axis=1, pct=True)
        turnover = ranks.diff().abs().mean(axis=1).dropna()
        return turnover

    # ------------------------------------------------------------------
    # Full test suite
    # ------------------------------------------------------------------

    def run_full_test(
        self,
        factor: pd.DataFrame,
        returns: pd.DataFrame,
    ) -> Dict[str, Any]:
        """Run the complete single-factor test suite.

        This is a convenience method that executes all individual tests and
        collects the results into a single dictionary.

        Args:
            factor: DataFrame of factor values ``(dates, symbols)``.
            returns: DataFrame of forward returns aligned with *factor*.

        Returns:
            Dictionary with keys ``"ic"``, ``"ic_decay"``, ``"quintile"``,
            and ``"turnover"``.
        """
        return {
            "ic": self.compute_ic(factor, returns),
            "ic_decay": self.compute_ic_decay(factor, returns),
            "quintile": self.quintile_analysis(factor, returns),
            "turnover": self.turnover_analysis(factor),
        }

    # ------------------------------------------------------------------
    # Summary formatting
    # ------------------------------------------------------------------

    @staticmethod
    def summary_stats(results: Dict[str, Any]) -> str:
        """Format test results as a human-readable summary string.

        Args:
            results: Output of :meth:`run_full_test`.

        Returns:
            Multi-line string summarising all test results.
        """
        ic: ICResult = results["ic"]
        quintile: QuintileResult = results["quintile"]
        turnover: pd.Series = results["turnover"]

        lines = [
            "=" * 60,
            "SINGLE FACTOR TEST SUMMARY",
            "=" * 60,
            f"  IC Mean:           {ic.ic_mean:>10.4f}",
            f"  IC Std:            {ic.ic_std:>10.4f}",
            f"  IR:                {ic.ir:>10.4f}",
            f"  Rank IC Mean:      {ic.rank_ic_mean:>10.4f}",
            f"  Rank IC Std:       {ic.rank_ic_std:>10.4f}",
            "",
            "Quintile Analysis:",
            f"  Q1 Mean Return:    {quintile.mean_returns.iloc[0]:>10.4f}",
            f"  Q{len(quintile.mean_returns)} Mean Return:    "
            f"{quintile.mean_returns.iloc[-1]:>10.4f}",
            f"  L/S Spread:        {quintile.spread:>10.4f}",
            f"  Monotonicity:      {quintile.monotonicity:>10.4f}",
            "",
            "Turnover:",
            f"  Mean Turnover:     {turnover.mean():>10.4f}",
            f"  Turnover Std:      {turnover.std():>10.4f}",
            "=" * 60,
        ]
        return "\n".join(lines)
