"""Standard backtest tear sheet analytics.

Takes an equity curve (and optionally a benchmark price series) and produces
the panel data a professional tear sheet renders: rebased equity comparison,
drawdown, rolling Sharpe, monthly return matrix, daily-return distribution,
and a stats table including alpha/beta vs the benchmark.

Pure computation — no I/O, no FastAPI. Consumers: dashboard backtest router,
research scripts, report generator.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

ROLL_WINDOW = 63  # ~3 trading months


def _to_daily(series: pd.Series) -> pd.Series:
    """Normalize any equity/price series to daily closes (tz-naive dates)."""
    s = series.dropna().copy()
    idx = pd.to_datetime(s.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    s.index = idx.normalize()
    return s.groupby(s.index).last().sort_index()


def _ann_factor(index: pd.DatetimeIndex) -> int:
    """365 for 7-day markets (crypto), 252 for exchange-calendar markets."""
    if len(index) < 10:
        return 252
    weekend_frac = (index.dayofweek >= 5).mean()
    return 365 if weekend_frac > 0.05 else 252


def _stats(returns: pd.Series, ann: int) -> Dict[str, Any]:
    if returns.std() == 0 or len(returns) < 2:
        return {"n_days": int(len(returns))}
    eq = (1 + returns).cumprod()
    years = len(returns) / ann
    cagr = float(eq.iloc[-1] ** (1 / years) - 1) if years > 0 else 0.0
    downside = returns[returns < 0]
    dd = eq / eq.cummax() - 1
    max_dd = float(dd.min())
    sharpe = float(returns.mean() / returns.std() * np.sqrt(ann))
    sortino = (float(returns.mean() / downside.std() * np.sqrt(ann))
               if len(downside) > 1 and downside.std() > 0 else None)
    return {
        "cagr": round(cagr, 4),
        "ann_vol": round(float(returns.std() * np.sqrt(ann)), 4),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2) if sortino is not None else None,
        "max_drawdown": round(max_dd, 4),
        "calmar": round(cagr / abs(max_dd), 2) if max_dd < 0 else None,
        "win_rate_days": round(float((returns > 0).mean()), 3),
        "best_day": round(float(returns.max()), 4),
        "worst_day": round(float(returns.min()), 4),
        "n_days": int(len(returns)),
    }


def compute_tearsheet(
    equity: pd.Series,
    benchmark: Optional[pd.Series] = None,
    benchmark_name: str = "",
    max_points: int = 400,
) -> Dict[str, Any]:
    """Build tear-sheet panel data from an equity curve.

    equity:    equity values indexed by timestamp (any frequency; resampled
               to daily).
    benchmark: benchmark PRICE series (e.g. SPY close); aligned to the
               strategy's date range.
    """
    eq = _to_daily(equity)
    if len(eq) < 3:
        return {"error": "equity curve too short for analytics",
                "n_points": int(len(eq))}
    rets = eq.pct_change().dropna()
    ann = _ann_factor(eq.index)

    out: Dict[str, Any] = {"ann_factor": ann, "benchmark": benchmark_name or None,
                           "period": {"start": str(eq.index[0].date()),
                                      "end": str(eq.index[-1].date())}}

    bench_eq = None
    if benchmark is not None and len(benchmark) > 2:
        b = _to_daily(benchmark)
        b = b.loc[(b.index >= eq.index[0]) & (b.index <= eq.index[-1])]
        if len(b) > 2:
            bench_eq = b

    # ── panel 1: rebased equity (base 100) ──
    step = max(1, len(eq) // max_points)
    eq_rb = (100 * eq / eq.iloc[0]).iloc[::step]
    curve = pd.DataFrame({"strategy": eq_rb})
    if bench_eq is not None:
        b_rb = 100 * bench_eq / bench_eq.iloc[0]
        curve["benchmark"] = b_rb.reindex(curve.index, method="ffill")
    out["equity"] = [
        {"date": str(d.date()),
         **{c: (round(float(v), 2) if pd.notna(v) else None)
            for c, v in row.items()}}
        for d, row in curve.iterrows()]

    # ── panel 2: drawdown ──
    dd = (eq / eq.cummax() - 1).iloc[::step]
    ddf = pd.DataFrame({"strategy": dd})
    if bench_eq is not None:
        bdd = (bench_eq / bench_eq.cummax() - 1)
        ddf["benchmark"] = bdd.reindex(ddf.index, method="ffill")
    out["drawdown"] = [
        {"date": str(d.date()),
         **{c: (round(float(v), 4) if pd.notna(v) else None)
            for c, v in row.items()}}
        for d, row in ddf.iterrows()]

    # ── panel 3: rolling Sharpe ──
    if len(rets) > ROLL_WINDOW:
        roll = (rets.rolling(ROLL_WINDOW).mean()
                / rets.rolling(ROLL_WINDOW).std() * np.sqrt(ann)).dropna()
        rstep = max(1, len(roll) // max_points)
        out["rolling_sharpe"] = [
            {"date": str(d.date()), "sharpe": round(float(v), 2)}
            for d, v in roll.iloc[::rstep].items() if np.isfinite(v)]
    else:
        out["rolling_sharpe"] = []

    # ── panel 4: monthly return matrix ──
    monthly = (1 + rets).groupby([rets.index.year, rets.index.month]).prod() - 1
    matrix: Dict[int, Dict[int, float]] = {}
    for (y, m), v in monthly.items():
        matrix.setdefault(int(y), {})[int(m)] = round(float(v), 4)
    out["monthly_returns"] = [
        {"year": y, "months": [matrix[y].get(m) for m in range(1, 13)],
         "total": round(float(np.prod([1 + x for x in matrix[y].values()]) - 1), 4)}
        for y in sorted(matrix)]

    # ── panel 5: daily-return distribution ──
    clipped = rets.clip(rets.quantile(0.005), rets.quantile(0.995))
    counts, edges = np.histogram(clipped, bins=40)
    out["distribution"] = {
        "bin_edges": [round(float(e), 5) for e in edges],
        "counts": [int(c) for c in counts]}

    # ── panel 6: stats table ──
    out["stats"] = {"strategy": _stats(rets, ann)}
    if bench_eq is not None:
        brets = bench_eq.pct_change().dropna()
        out["stats"]["benchmark"] = _stats(brets, ann)
        joined = pd.concat([rets, brets], axis=1, join="inner").dropna()
        joined.columns = ["s", "b"]
        if len(joined) > ROLL_WINDOW and joined["b"].var() > 0:
            beta = float(joined["s"].cov(joined["b"]) / joined["b"].var())
            alpha_ann = float((joined["s"].mean() - beta * joined["b"].mean()) * ann)
            out["stats"]["relative"] = {
                "beta": round(beta, 3),
                "alpha_ann": round(alpha_ann, 4),
                "correlation": round(float(joined["s"].corr(joined["b"])), 3),
                "n_overlap_days": int(len(joined)),
            }
    return out
