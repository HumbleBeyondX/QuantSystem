"""Industry-standard cross-sectional factor evaluator.

Given a factor panel (date × ticker) and forward returns at multiple
horizons, computes the full metric suite expected by professional
quant teams:

  Core IC metrics
    - ic_mean / ic_std / ic_ir / ic_t_stat
    - rank_ic_mean / rank_ic_ir / rank_ic_t_stat
    - ic_decay_curve       — IC at horizons [1, 5, 10, 21, 63]
    - autocorr_1d          — factor-value 1-day autocorrelation (stickiness)

  Quintile / long-short
    - q1..q5_ret_ann       — annualized return of each quintile
    - quintile_spread_bps  — Q5 − Q1 spread (bps annualized)
    - monotonicity         — Spearman corr between quintile rank and avg return
    - ls_return_ann        — long Q5 / short Q1 portfolio return
    - ls_vol_ann
    - ls_sharpe
    - ls_max_dd
    - ls_turnover_ann

  Coverage
    - coverage_pct         — % of (date, ticker) cells with valid factor
    - n_valid_dates / n_obs

All inputs assumed to use a 1-day lag for trade execution (signal at
t consumed at close of t, returns realized t+1 through t+horizon).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
from scipy import stats


DEFAULT_HORIZONS: List[int] = [1, 5, 10, 21, 63]


@dataclass
class FactorMetrics:
    factor_name: str
    universe: str
    period_start: str
    period_end: str

    # Core IC
    ic_mean: float = 0.0
    ic_std: float = 0.0
    ic_ir: float = 0.0
    ic_t_stat: float = 0.0
    rank_ic_mean: float = 0.0
    rank_ic_std: float = 0.0
    rank_ic_ir: float = 0.0
    rank_ic_t_stat: float = 0.0

    # Decay
    decay: Dict[int, float] = field(default_factory=dict)
    decay_rank: Dict[int, float] = field(default_factory=dict)

    # Coverage
    coverage_pct: float = 0.0
    n_obs: int = 0
    n_valid_dates: int = 0
    autocorr_1d: float = 0.0

    # Quintile / long-short (computed on best-IC horizon)
    best_horizon: int = 1
    q1_ret_ann: float = 0.0
    q2_ret_ann: float = 0.0
    q3_ret_ann: float = 0.0
    q4_ret_ann: float = 0.0
    q5_ret_ann: float = 0.0
    quintile_spread_bps: float = 0.0
    monotonicity: float = 0.0
    ls_return_ann: float = 0.0
    ls_vol_ann: float = 0.0
    ls_sharpe: float = 0.0
    ls_max_dd: float = 0.0
    ls_turnover_ann: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ────────────────────────────────────────────────────────────
# Core IC computation
# ────────────────────────────────────────────────────────────

def _per_date_ic(
    factor_row: pd.Series,
    fwd_ret_row: pd.Series,
    rank: bool = False,
) -> float:
    """One day's cross-sectional IC."""
    df = pd.DataFrame({"f": factor_row, "r": fwd_ret_row}).dropna()
    if len(df) < 4:
        return np.nan
    if rank:
        return float(df["f"].corr(df["r"], method="spearman"))
    return float(df["f"].corr(df["r"]))


def cross_sectional_ic_series(
    factor: pd.DataFrame,
    fwd_returns: pd.DataFrame,
    rank: bool = False,
) -> pd.Series:
    """One IC value per date — vectorized.

    Computes per-row Pearson (or Spearman) correlation between two
    aligned wide DataFrames using broadcast arithmetic. Much faster than
    a Python-level row loop.
    """
    common = factor.index.intersection(fwd_returns.index)
    f = factor.loc[common]
    r = fwd_returns.loc[common]
    common_cols = f.columns.intersection(r.columns)
    f = f[common_cols]
    r = r[common_cols]

    if rank:
        # Cross-sectional rank per row (NaN preserved)
        f = f.rank(axis=1)
        r = r.rank(axis=1)

    # Mask: both must be non-NaN
    mask = f.notna() & r.notna()
    n = mask.sum(axis=1).astype(float)

    f = f.where(mask)
    r = r.where(mask)

    f_mean = f.mean(axis=1, skipna=True)
    r_mean = r.mean(axis=1, skipna=True)
    f_dev = f.sub(f_mean, axis=0)
    r_dev = r.sub(r_mean, axis=0)

    cov = (f_dev * r_dev).sum(axis=1, skipna=True) / (n - 1).replace(0, np.nan)
    f_std = (f_dev.pow(2).sum(axis=1, skipna=True) / (n - 1).replace(0, np.nan)).pow(0.5)
    r_std = (r_dev.pow(2).sum(axis=1, skipna=True) / (n - 1).replace(0, np.nan)).pow(0.5)

    ic = cov / (f_std * r_std).replace(0, np.nan)
    # Need at least 4 observations to be meaningful
    ic = ic.where(n >= 4)
    return ic.dropna()


# ────────────────────────────────────────────────────────────
# Quintile portfolio backtest
# ────────────────────────────────────────────────────────────

def quintile_returns(
    factor: pd.DataFrame,
    fwd_returns_1d: pd.DataFrame,
    n_quantiles: int = 5,
) -> pd.DataFrame:
    """For each date, sort tickers into quintiles by factor value, then
    compute equal-weighted next-day return per quintile.

    Vectorized: rank tickers per row → divide rank into quantile bins →
    per-bin mean of forward returns.
    """
    common_idx = factor.index.intersection(fwd_returns_1d.index)
    f = factor.loc[common_idx]
    r = fwd_returns_1d.loc[common_idx]

    common_cols = f.columns.intersection(r.columns)
    f = f[common_cols]
    r = r[common_cols]

    # Rank cross-sectionally per row (1..n_tickers)
    valid_count = f.notna().sum(axis=1)
    ranks = f.rank(axis=1, method="first")

    # Quantile bins: ceil(rank / n_per_q) where n_per_q = valid_count / n_quantiles
    n_per_q = valid_count / n_quantiles
    # Use broadcasting: bin_id = ceil(rank / n_per_q), capped at n_quantiles
    bin_id = np.ceil(ranks.div(n_per_q, axis=0)).clip(upper=n_quantiles)

    out = pd.DataFrame(index=common_idx,
                       columns=[f"q{i+1}" for i in range(n_quantiles)],
                       dtype="float64")
    for q in range(1, n_quantiles + 1):
        mask = (bin_id == q)
        # Per-row mean of fwd returns where mask is True
        masked_r = r.where(mask)
        out[f"q{q}"] = masked_r.mean(axis=1, skipna=True)

    # Drop rows where there weren't enough valid tickers
    out.loc[valid_count < n_quantiles * 2] = np.nan
    return out


def long_short_metrics(
    quintile_rets: pd.DataFrame,
    factor: pd.DataFrame,
    annualize: int = 252,
) -> Dict[str, float]:
    """Compute long Q5 − short Q1 portfolio metrics.

    Also turnover: avg fraction of names that change between top quintile
    today and top quintile tomorrow.
    """
    qrets = quintile_rets.dropna(how="all")
    if qrets.empty:
        return {}

    # Per-quintile annualized return
    q_means = qrets.mean()
    q_ann = ((1 + q_means).pow(annualize) - 1).to_dict()

    # Long-short P&L
    if "q5" not in qrets.columns or "q1" not in qrets.columns:
        return {}
    ls = qrets["q5"].dropna() - qrets["q1"].dropna()
    ls = ls.dropna()
    if len(ls) < 30:
        return {}
    ls_mean = ls.mean()
    ls_std = ls.std()
    ls_ann_ret = ls_mean * annualize
    ls_ann_vol = ls_std * np.sqrt(annualize)
    ls_sharpe = ls_ann_ret / ls_ann_vol if ls_ann_vol > 0 else 0.0

    # Equity curve & max DD
    equity = (1 + ls).cumprod()
    running_max = equity.cummax()
    dd = (equity / running_max - 1).min()

    # Quintile spread in basis points (annualized)
    spread_bps = (q_means["q5"] - q_means["q1"]) * annualize * 10_000

    # Monotonicity: Spearman corr of (1..5) vs (q1..q5) means
    rank_arr = list(range(1, 6))
    means_arr = [q_means.get(f"q{i}", np.nan) for i in range(1, 6)]
    if all(not np.isnan(m) for m in means_arr):
        mono = stats.spearmanr(rank_arr, means_arr).correlation
    else:
        mono = np.nan

    # Turnover: track top-quintile membership changes day-over-day
    turnover = _quintile_turnover(factor, n_quantiles=5)

    return {
        "q1_ret_ann": float(q_ann.get("q1", 0)),
        "q2_ret_ann": float(q_ann.get("q2", 0)),
        "q3_ret_ann": float(q_ann.get("q3", 0)),
        "q4_ret_ann": float(q_ann.get("q4", 0)),
        "q5_ret_ann": float(q_ann.get("q5", 0)),
        "quintile_spread_bps": float(spread_bps),
        "monotonicity": float(mono) if not np.isnan(mono) else 0.0,
        "ls_return_ann": float(ls_ann_ret),
        "ls_vol_ann": float(ls_ann_vol),
        "ls_sharpe": float(ls_sharpe),
        "ls_max_dd": float(abs(dd)),
        "ls_turnover_ann": float(turnover * annualize),
    }


def _quintile_turnover(factor: pd.DataFrame, n_quantiles: int = 5) -> float:
    """Average fraction of top-quintile that changes day-over-day (vectorized)."""
    valid_count = factor.notna().sum(axis=1)
    ranks = factor.rank(axis=1, method="first")
    n_per_q = valid_count / n_quantiles
    bin_id = np.ceil(ranks.div(n_per_q, axis=0)).clip(upper=n_quantiles)
    top_mask = (bin_id == n_quantiles)

    valid_rows = top_mask.sum(axis=1) > 0
    top_mask = top_mask[valid_rows]

    if len(top_mask) < 2:
        return 0.0

    # Fraction "new" between consecutive rows: (cur ∧ ¬prev) / |prev|
    prev = top_mask.shift(1).fillna(False).astype(bool)
    cur = top_mask.astype(bool)
    new_count = (cur & ~prev).sum(axis=1)
    prev_count = prev.sum(axis=1).replace(0, np.nan)
    daily_turnover = (new_count / prev_count).dropna()
    return float(daily_turnover.mean()) if len(daily_turnover) else 0.0


# ────────────────────────────────────────────────────────────
# Top-level evaluator
# ────────────────────────────────────────────────────────────

def evaluate_factor(
    factor_panel: pd.DataFrame,
    panel,                         # DataPanel
    *,
    factor_name: str = "",
    universe: str = "",
    horizons: Optional[List[int]] = None,
    rebalance_lag: int = 1,
) -> FactorMetrics:
    """Run the full industry-standard evaluation.

    Args:
        factor_panel: wide DataFrame (date × ticker) of factor values.
        panel: DataPanel — used to compute forward returns.
        horizons: forward-return horizons (in days).
        rebalance_lag: signal at t consumed at t+lag (default 1).

    Returns:
        FactorMetrics dataclass with all metrics populated.
    """
    horizons = horizons or DEFAULT_HORIZONS

    # Align factor with panel data range
    factor = factor_panel.copy()
    common_cols = factor.columns.intersection(panel.closes_wide.columns)
    factor = factor[common_cols]

    period_start = str(factor.index.min())
    period_end = str(factor.index.max())

    metrics = FactorMetrics(
        factor_name=factor_name,
        universe=universe,
        period_start=period_start,
        period_end=period_end,
    )

    # Coverage
    total_cells = factor.size
    valid_cells = factor.notna().sum().sum()
    metrics.coverage_pct = float(valid_cells / total_cells * 100) if total_cells > 0 else 0.0
    metrics.n_obs = int(valid_cells)
    metrics.n_valid_dates = int((factor.notna().sum(axis=1) > 0).sum())

    if metrics.n_valid_dates < 60:
        return metrics  # not enough data

    # Autocorr (factor stickiness)
    daily_factor_mean = factor.mean(axis=1).dropna()
    if len(daily_factor_mean) >= 30:
        metrics.autocorr_1d = float(daily_factor_mean.autocorr(lag=1) or 0.0)

    # ── IC for each horizon ──
    ic_by_horizon: Dict[int, float] = {}
    rank_ic_by_horizon: Dict[int, float] = {}
    best_h = horizons[0]
    best_abs_ir = -1.0

    for h in horizons:
        # Apply rebalance lag: signal at t, return realized t+lag → t+lag+h
        # Equivalent: shift factor forward by lag, then compare to fwd return h
        fwd = panel.forward_returns(horizon=h, kind="log")
        # Apply lag: factor at t corresponds to forward-return starting at t+lag
        factor_lagged = factor.shift(rebalance_lag)

        ic_series = cross_sectional_ic_series(factor_lagged, fwd, rank=False)
        rank_ic_series = cross_sectional_ic_series(factor_lagged, fwd, rank=True)

        ic_by_horizon[h] = float(ic_series.mean()) if len(ic_series) else 0.0
        rank_ic_by_horizon[h] = float(rank_ic_series.mean()) if len(rank_ic_series) else 0.0

        # If this is the primary horizon (1d), record full stats
        if h == 1:
            if len(ic_series) >= 30:
                metrics.ic_mean = float(ic_series.mean())
                metrics.ic_std = float(ic_series.std())
                metrics.ic_ir = (
                    metrics.ic_mean / metrics.ic_std if metrics.ic_std > 0 else 0.0
                )
                t_ic = stats.ttest_1samp(ic_series.dropna().values, 0.0)
                metrics.ic_t_stat = float(t_ic.statistic)
            if len(rank_ic_series) >= 30:
                metrics.rank_ic_mean = float(rank_ic_series.mean())
                metrics.rank_ic_std = float(rank_ic_series.std())
                metrics.rank_ic_ir = (
                    metrics.rank_ic_mean / metrics.rank_ic_std
                    if metrics.rank_ic_std > 0 else 0.0
                )
                t_rank = stats.ttest_1samp(rank_ic_series.dropna().values, 0.0)
                metrics.rank_ic_t_stat = float(t_rank.statistic)

        # Track best horizon by absolute IR
        if len(ic_series) >= 30:
            ir = (ic_series.mean() / ic_series.std()) if ic_series.std() > 0 else 0.0
            if abs(ir) > best_abs_ir:
                best_abs_ir = abs(ir)
                best_h = h

    metrics.decay = ic_by_horizon
    metrics.decay_rank = rank_ic_by_horizon
    metrics.best_horizon = best_h

    # ── Quintile / long-short on best horizon ──
    fwd_best = panel.forward_returns(horizon=best_h, kind="log")
    factor_lagged = factor.shift(rebalance_lag)
    q_rets = quintile_returns(factor_lagged, fwd_best)
    ls = long_short_metrics(q_rets, factor_lagged, annualize=252 // max(best_h, 1))
    for k, v in ls.items():
        if hasattr(metrics, k):
            setattr(metrics, k, v)

    return metrics


# ────────────────────────────────────────────────────────────
# Decorrelation utilities
# ────────────────────────────────────────────────────────────

def factor_correlation_matrix(
    factor_panels: Dict[str, pd.DataFrame],
    *,
    use_ranks: bool = True,
) -> pd.DataFrame:
    """Pairwise correlation of factors across all (date, ticker) cells.

    Args:
        factor_panels: dict factor_name → wide DataFrame.
        use_ranks: if True, compute rank correlations (more robust).
    """
    # Stack each factor to a single Series indexed by (date, ticker)
    flat = {}
    for name, df in factor_panels.items():
        s = df.stack(future_stack=True) if hasattr(df, "stack") else df.stack()
        flat[name] = s

    # Align all
    combined = pd.DataFrame(flat).dropna(how="all")
    if use_ranks:
        combined = combined.rank()
    return combined.corr(method="spearman" if use_ranks else "pearson")


def cluster_factors(
    corr_matrix: pd.DataFrame,
    threshold: float = 0.7,
) -> Dict[int, List[str]]:
    """Hierarchical clustering: factors with corr > threshold grouped.

    Returns {cluster_id: [factor_name, ...]}. If the input has any NaN
    rows/cols (factors with no overlap), they're dropped before clustering
    and added back as singleton clusters.
    """
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import squareform

    # Drop factors that are entirely NaN in the corr matrix
    valid = corr_matrix.dropna(how="all").dropna(axis=1, how="all")
    valid = valid.loc[valid.index, valid.index]
    if valid.empty or valid.shape[0] < 2:
        return {i + 1: [name] for i, name in enumerate(corr_matrix.index)}

    # Fill any remaining NaN (factors that have data but degenerate against one
    # another) with 0 — they'll be treated as orthogonal.
    valid = valid.fillna(0.0)

    # Distance = 1 - |corr|
    dist = 1 - valid.abs()
    dist = (dist + dist.T) / 2
    np.fill_diagonal(dist.values, 0.0)

    condensed = squareform(dist.values, checks=False)
    if not np.all(np.isfinite(condensed)):
        return {i + 1: [name] for i, name in enumerate(corr_matrix.index)}

    Z = linkage(condensed, method="average")
    clusters = fcluster(Z, t=1 - threshold, criterion="distance")

    out: Dict[int, List[str]] = {}
    for name, cid in zip(valid.index, clusters):
        out.setdefault(int(cid), []).append(name)

    # Add factors that were dropped as singletons
    next_cid = max(out.keys(), default=0) + 1
    for name in corr_matrix.index:
        if name not in valid.index:
            out[next_cid] = [name]
            next_cid += 1
    return out


def select_top_per_cluster(
    metrics_by_factor: Dict[str, FactorMetrics],
    clusters: Dict[int, List[str]],
    metric_attr: str = "rank_ic_ir",
) -> List[str]:
    """From each cluster, pick the factor with highest |metric|."""
    selected = []
    for cid, names in clusters.items():
        best_name = None
        best_score = -1.0
        for n in names:
            if n in metrics_by_factor:
                score = abs(getattr(metrics_by_factor[n], metric_attr, 0.0))
                if score > best_score:
                    best_score = score
                    best_name = n
        if best_name:
            selected.append(best_name)
    return selected
