"""Factor evaluation — IC, Rank IC, IR, decay, quintile spread.

Given factor values and forward returns over the same index, answer:

  1. Does the factor predict future return? (IC)
  2. Is it robust to outliers? (Rank IC)
  3. Is the signal stable over time? (IR = mean(IC) / std(IC))
  4. At what horizon is it strongest? (IC decay curve)
  5. Does a tradable long/short spread exist? (Quintile spread)
  6. Is the result statistically significant? (t-stat on mean IC)

All functions are pure — pass pd.Series in, get metrics out. The batch
runner in ``scripts/evaluate_factors.py`` wraps this to produce DB rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from scipy import stats


DEFAULT_HORIZONS = [1, 4, 8, 24, 48]  # bars (1h, 4h, 8h, 24h, 48h for 1H data)


@dataclass
class HorizonResult:
    horizon_bars: int
    ic_mean: float
    ic_std: float
    ir: float                 # Information Ratio
    rank_ic: float            # Spearman, more robust to outliers
    t_stat: float             # t-statistic of mean IC vs zero
    quintile_spread_bps: float  # long top-20%, short bottom-20%, annualized bps
    n_obs: int                # number of valid (factor, return) pairs


@dataclass
class FactorEvalResult:
    factor_name: str
    category: str
    symbol: str
    timeframe: str
    start_date: str
    end_date: str
    by_horizon: Dict[int, HorizonResult] = field(default_factory=dict)
    ic_series: List[float] = field(default_factory=list)  # rolling IC for plotting

    def best_horizon(self) -> Optional[HorizonResult]:
        """Return the horizon with the largest |IR|."""
        if not self.by_horizon:
            return None
        return max(self.by_horizon.values(), key=lambda r: abs(r.ir))

    def to_dict(self) -> dict:
        return {
            "factor_name": self.factor_name,
            "category": self.category,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "by_horizon": {str(h): asdict(r) for h, r in self.by_horizon.items()},
            "ic_series": self.ic_series,
        }


# ────────────────────────────────────────────────────────────
# Core metric functions
# ────────────────────────────────────────────────────────────

def forward_return(close: pd.Series, horizon: int) -> pd.Series:
    """Log return over the next `horizon` bars (aligned to t, so it's the
    return realized between t and t+horizon)."""
    return np.log(close.shift(-horizon) / close)


def information_coefficient(
    factor_values: pd.Series,
    forward_rets: pd.Series,
) -> float:
    """Pearson correlation between factor at t and forward return t→t+h.

    Returns NaN if < 30 valid paired observations (too noisy to trust).
    """
    df = pd.DataFrame({"f": factor_values, "r": forward_rets}).dropna()
    if len(df) < 30:
        return np.nan
    return float(df["f"].corr(df["r"]))


def rank_information_coefficient(
    factor_values: pd.Series,
    forward_rets: pd.Series,
) -> float:
    """Spearman rank correlation. More robust to non-linear / heavy-tailed data."""
    df = pd.DataFrame({"f": factor_values, "r": forward_rets}).dropna()
    if len(df) < 30:
        return np.nan
    return float(df["f"].corr(df["r"], method="spearman"))


def quintile_spread_bps(
    factor_values: pd.Series,
    forward_rets: pd.Series,
    annualization_factor: float = 1.0,
) -> float:
    """Long top-20% minus short bottom-20% of factor, in basis points.

    Returns the spread in bps per bar (not annualized unless caller
    passes annualization_factor). For 1H bars, pass ~8766 to annualize.
    """
    df = pd.DataFrame({"f": factor_values, "r": forward_rets}).dropna()
    if len(df) < 100:
        return float("nan")

    # Assign each row a quintile within the full sample (simple approach;
    # a time-series-aware version would bucket within rolling windows,
    # but for ~20k 1H bars the full-sample version is a good first pass).
    try:
        df["q"] = pd.qcut(df["f"].rank(method="first"), 5, labels=False, duplicates="drop")
    except ValueError:
        # Factor has too few distinct values for quintiles
        return float("nan")

    top = df[df["q"] == 4]["r"].mean()
    bot = df[df["q"] == 0]["r"].mean()
    spread = top - bot
    return float(spread * annualization_factor * 10_000)


def ic_t_stat(
    factor_values: pd.Series,
    forward_rets: pd.Series,
    window: int = 500,
) -> tuple:
    """Rolling IC series + t-statistic of its mean vs zero.

    Returns (ic_series as list, t_stat).
    """
    df = pd.DataFrame({"f": factor_values, "r": forward_rets}).dropna()
    if len(df) < window * 2:
        # Too few obs for rolling — fall back to a single IC
        ic = information_coefficient(factor_values, forward_rets)
        return ([ic] if not np.isnan(ic) else [], 0.0)

    rolling_ic = (
        df["f"].rolling(window, min_periods=window // 2).corr(df["r"]).dropna()
    )
    if len(rolling_ic) < 10:
        return (rolling_ic.tolist(), 0.0)

    t = stats.ttest_1samp(rolling_ic.values, 0.0)
    return (rolling_ic.tolist(), float(t.statistic))


# ────────────────────────────────────────────────────────────
# Top-level evaluator
# ────────────────────────────────────────────────────────────

def evaluate_factor(
    factor_name: str,
    category: str,
    factor_values: pd.Series,
    close: pd.Series,
    *,
    symbol: str = "ETH/USDT",
    timeframe: str = "1h",
    start_date: str = "",
    end_date: str = "",
    horizons: Optional[List[int]] = None,
    rolling_ic_window: int = 500,
    annualization_factor: float = 8766.0,  # 1H → annual (365.25*24)
) -> FactorEvalResult:
    """Run the full evaluation and return a structured result.

    Skips horizons where fewer than 100 valid observations are available.
    """
    horizons = horizons or DEFAULT_HORIZONS
    result = FactorEvalResult(
        factor_name=factor_name,
        category=category,
        symbol=symbol,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
    )

    for h in horizons:
        fwd = forward_return(close, h)
        paired = pd.DataFrame({"f": factor_values, "r": fwd}).dropna()
        if len(paired) < 100:
            continue

        ic = information_coefficient(paired["f"], paired["r"])
        rank_ic = rank_information_coefficient(paired["f"], paired["r"])
        qs = quintile_spread_bps(paired["f"], paired["r"], annualization_factor=annualization_factor)
        ic_series, t_stat = ic_t_stat(paired["f"], paired["r"], window=rolling_ic_window)

        # IR — mean(rolling IC) / std(rolling IC)
        if len(ic_series) > 1:
            ic_mean = float(np.nanmean(ic_series))
            ic_std = float(np.nanstd(ic_series))
            ir = ic_mean / ic_std if ic_std > 0 else 0.0
        else:
            ic_mean = ic if not np.isnan(ic) else 0.0
            ic_std = 0.0
            ir = 0.0

        result.by_horizon[h] = HorizonResult(
            horizon_bars=h,
            ic_mean=ic_mean,
            ic_std=ic_std,
            ir=float(ir),
            rank_ic=rank_ic if not np.isnan(rank_ic) else 0.0,
            t_stat=t_stat,
            quintile_spread_bps=qs if not np.isnan(qs) else 0.0,
            n_obs=len(paired),
        )

        # Store rolling IC only for the primary horizon (first one)
        if h == horizons[0]:
            result.ic_series = ic_series

    return result


def format_summary_row(r: FactorEvalResult) -> Dict[str, float]:
    """Flatten a FactorEvalResult to a single-row dict for summary tables."""
    best = r.best_horizon()
    if best is None:
        return {
            "factor": r.factor_name, "category": r.category,
            "best_h": None, "ir": 0.0, "rank_ic": 0.0,
            "ic_mean": 0.0, "ic_std": 0.0, "t_stat": 0.0,
            "qspread_bps": 0.0, "n_obs": 0,
        }
    return {
        "factor": r.factor_name,
        "category": r.category,
        "best_h": best.horizon_bars,
        "ir": round(best.ir, 3),
        "rank_ic": round(best.rank_ic, 4),
        "ic_mean": round(best.ic_mean, 4),
        "ic_std": round(best.ic_std, 4),
        "t_stat": round(best.t_stat, 2),
        "qspread_bps": round(best.quintile_spread_bps, 1),
        "n_obs": best.n_obs,
    }
